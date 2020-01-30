"""A Quantizer defines the way of transforming a full precision input to a
quantized output and the pseudo-gradient method used for the backwards pass.

Quantizers can either be used through quantizer arguments that are supported
for Larq layers, such as `input_quantizer` and `kernel_quantizer`; or they
can be used similar to activations, i.e. either through an `Activation` layer,
or through the `activation` argument supported by all forward layers:

```python
import tensorflow as tf
import larq as lq
...
x = lq.layers.QuantDense(64, activation=None)(x)
x = lq.layers.QuantDense(64, input_quantizer="ste_sign")(x)
```

is equivalent to:

```python
x = lq.layers.QuantDense(64)(x)
x = tf.keras.layers.Activation("ste_sign")(x)
x = lq.layers.QuantDense(64)(x)
```

as well as:

```python
x = lq.layers.QuantDense(64, activation="ste_sign")(x)
x = lq.layers.QuantDense(64)(x)
```

We highly recommend using the first of these formulations: for the
other two formulations, intermediate layers - like batch normalization or
average pooling - and shortcut connections may result in non-binary input
to the convolutions.

Quantizers can either be referenced by string or called directly.
The following usages are equivalent:

```python
lq.layers.QuantDense(64, kernel_quantizer="ste_sign")
```
```python
lq.layers.QuantDense(64, kernel_quantizer=lq.quantizers.SteSign(clip_value=1.0))
```
"""

from typing import Callable, Union

import tensorflow as tf

from larq import math, metrics as lq_metrics, utils

__all__ = [
    "SteSign",
    "ApproxSign",
    "MagnitudeAwareSign",
    "SwishSign",
    "SteTern",
    "SteHeaviside",
    "DoReFaQuantizer",
]


def _clipped_gradient(x, dy, clip_value):
    """Calculate `clipped_gradent * dy`."""

    if clip_value is None:
        return dy

    zeros = tf.zeros_like(dy)
    mask = tf.math.less_equal(tf.math.abs(x), clip_value)
    return tf.where(mask, dy, zeros)


def ste_sign(x: tf.Tensor, clip_value: float = 1.0) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        def grad(dy):
            return _clipped_gradient(x, dy, clip_value)

        return math.sign(x), grad

    return _call(x)


def _scaled_sign(x):  # pragma: no cover
    return 1.3 * ste_sign(x)


@tf.custom_gradient
def approx_sign(x: tf.Tensor) -> tf.Tensor:
    def grad(dy):
        abs_x = tf.math.abs(x)
        zeros = tf.zeros_like(dy)
        mask = tf.math.less_equal(abs_x, 1.0)
        return tf.where(mask, (1 - abs_x) * 2 * dy, zeros)

    return math.sign(x), grad


def swish_sign(x: tf.Tensor, beta: float = 5.0) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        def grad(dy):
            b_x = beta * x
            return dy * beta * (2 - b_x * tf.tanh(b_x * 0.5)) / (1 + tf.cosh(b_x))

        return math.sign(x), grad

    return _call(x)


def ste_tern(
    x: tf.Tensor,
    threshold_value: float = 0.05,
    ternary_weight_networks: bool = False,
    clip_value: float = 1.0,
) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        if ternary_weight_networks:
            threshold = 0.7 * tf.reduce_sum(tf.abs(x)) / tf.cast(tf.size(x), x.dtype)
        else:
            threshold = threshold_value

        def grad(dy):
            return _clipped_gradient(x, dy, clip_value)

        return tf.sign(tf.sign(x + threshold) + tf.sign(x - threshold)), grad

    return _call(x)


def ste_heaviside(x: tf.Tensor, clip_value: float = 1.0) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        def grad(dy):
            return _clipped_gradient(x, dy, clip_value)

        return math.heaviside(x), grad

    return _call(x)


class BaseQuantizer(tf.keras.layers.Layer):
    """Base class for defining quantizers with Larq metrics."""

    def __init__(self, *args, metrics=None, **kwargs):
        self._custom_metrics = (
            metrics if metrics is not None else lq_metrics.get_training_metrics()
        )

        super().__init__(*args, **kwargs)

    def build(self, input_shape):
        super().build(input_shape)

        if "flip_ratio" in self._custom_metrics:
            self.flip_ratio = lq_metrics.FlipRatio(name=f"flip_ratio/{self.name}")

    def call(self, inputs):
        if hasattr(self, "flip_ratio"):

            # If the flip ratio hasn't been built, first check if this is a weights
            # quantizer; if not, remove the flip ratio.
            if not self.flip_ratio.built and inputs.shape[0] is None:
                delattr(self, "flip_ratio")

            # Otherwise compute the flip ratio and
            else:
                self.add_metric(self.flip_ratio(inputs))


@utils.register_alias("ste_sign")
@utils.register_keras_custom_object
class SteSign(BaseQuantizer):
    r"""Instantiates a serializable binary quantizer.

    \\[
    q(x) = \begin{cases}
      -1 & x < 0 \\\
      1 & x \geq 0
    \end{cases}
    \\]

    The gradient is estimated using the Straight-Through Estimator
    (essentially the binarization is replaced by a clipped identity on the
    backward pass).
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
      1 & \left|x\right| \leq \texttt{clip_value} \\\
      0 & \left|x\right| > \texttt{clip_value}
    \end{cases}\\]

    ```plot-activation
    quantizers.SteSign
    ```

    # Arguments
    clip_value: Threshold for clipping gradients. If `None` gradients are not clipped.
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # References
    - [Binarized Neural Networks: Training Deep Neural Networks with Weights and
      Activations Constrained to +1 or -1](http://arxiv.org/abs/1602.02830)
    """
    precision = 1

    def __init__(self, clip_value: float = 1.0, **kwargs):
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        super().call(inputs)
        return ste_sign(inputs, clip_value=self.clip_value)

    def get_config(self):
        return {**super().get_config(), "clip_value": self.clip_value}


@utils.register_alias("approx_sign")
@utils.register_keras_custom_object
class ApproxSign(BaseQuantizer):
    r"""Instantiates a serializable binary quantizer.
    \\[
    q(x) = \begin{cases}
      -1 & x < 0 \\\
      1 & x \geq 0
    \end{cases}
    \\]

    The gradient is estimated using the ApproxSign method.
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
      (2 - 2 \left|x\right|) & \left|x\right| \leq 1 \\\
      0 & \left|x\right| > 1
    \end{cases}
    \\]

    ```plot-activation
    quantizers.ApproxSign
    ```

    # Arguments
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # References
    - [Bi-Real Net: Enhancing the Performance of 1-bit CNNs With Improved
      Representational Capability and Advanced
      Training Algorithm](http://arxiv.org/abs/1808.00278)
    """
    precision = 1

    def call(self, inputs):
        super().call(inputs)
        return approx_sign(inputs)


@utils.register_alias("ste_heaviside")
@utils.register_keras_custom_object
class SteHeaviside(BaseQuantizer):
    r"""
    Instantiates a binarization quantizer with output values 0 and 1.
    \\[
    q(x) = \begin{cases}
    +1 & x > 0 \\\
    0 & x \leq 0
    \end{cases}
    \\]

    The gradient is estimated using the Straight-Through Estimator
    (essentially the binarization is replaced by a clipped identity on the
    backward pass).

    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
    1 & \left|x\right| \leq 1 \\\
    0 & \left|x\right| > 1
    \end{cases}\\]

    ```plot-activation
    quantizers.SteHeaviside
    ```

    # Arguments
    clip_value: Threshold for clipping gradients. If `None` gradients are not clipped.
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # Returns
    AND Binarization function
    """
    precision = 1

    def __init__(self, clip_value: float = 1.0, **kwargs):
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        super().call(inputs)
        return ste_heaviside(inputs, clip_value=self.clip_value)

    def get_config(self):
        return {**super().get_config(), "clip_value": self.clip_value}


@utils.register_alias("swish_sign")
@utils.register_keras_custom_object
class SwishSign(BaseQuantizer):
    r"""Sign binarization function.

    \\[
    q(x) = \begin{cases}
      -1 & x < 0 \\\
      1 & x \geq 0
    \end{cases}
    \\]

    The gradient is estimated using the SignSwish method.

    \\[
    \frac{\partial q_{\beta}(x)}{\partial x} = \frac{\beta\left\\{2-\beta x \tanh \left(\frac{\beta x}{2}\right)\right\\}}{1+\cosh (\beta x)}
    \\]

    ```plot-activation
    quantizers.SwishSign
    ```
    # Arguments
    beta: Larger values result in a closer approximation to the derivative of the sign.
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # Returns
    SwishSign quantization function

    # References
    - [BNN+: Improved Binary Network Training](https://arxiv.org/abs/1812.11800)
    """
    precision = 1

    def __init__(self, beta: float = 5.0, **kwargs):
        self.beta = beta
        super().__init__(**kwargs)

    def call(self, inputs):
        super().call(inputs)
        return swish_sign(inputs, beta=self.beta)

    def get_config(self):
        return {**super().get_config(), "beta": self.beta}


@utils.register_alias("magnitude_aware_sign")
@utils.register_keras_custom_object
class MagnitudeAwareSign(BaseQuantizer):
    r"""Instantiates a serializable magnitude-aware sign quantizer for Bi-Real Net.

    A scaled sign function computed according to Section 3.3 in
    [Zechun Liu et al](https://arxiv.org/abs/1808.00278).

    ```plot-activation
    quantizers._scaled_sign
    ```

    # Arguments
    clip_value: Threshold for clipping gradients. If `None` gradients are not clipped.
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # References
    - [Bi-Real Net: Enhancing the Performance of 1-bit CNNs With Improved
      Representational Capability and Advanced Training
      Algorithm](https://arxiv.org/abs/1808.00278)

    """
    precision = 1

    def __init__(self, clip_value: float = 1.0, **kwargs):
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        super().call(inputs)
        scale_factor = tf.stop_gradient(
            tf.reduce_mean(tf.abs(inputs), axis=list(range(len(inputs.shape) - 1)))
        )

        return scale_factor * ste_sign(inputs, clip_value=self.clip_value)

    def get_config(self):
        return {**super().get_config(), "clip_value": self.clip_value}


@utils.register_alias("ste_tern")
@utils.register_keras_custom_object
class SteTern(BaseQuantizer):
    r"""Instantiates a serializable ternarization quantizer.

    \\[
    q(x) = \begin{cases}
    +1 & x > \Delta \\\
    0 & |x| < \Delta \\\
     -1 & x < - \Delta
    \end{cases}
    \\]

    where \\(\Delta\\) is defined as the threshold and can be passed as an argument,
    or can be calculated as per the Ternary Weight Networks original paper, such that

    \\[
    \Delta = \frac{0.7}{n} \sum_{i=1}^{n} |W_i|
    \\]
    where we assume that \\(W_i\\) is generated from a normal distribution.

    The gradient is estimated using the Straight-Through Estimator
    (essentially the Ternarization is replaced by a clipped identity on the
    backward pass).
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
    1 & \left|x\right| \leq \texttt{clip_value} \\\
    0 & \left|x\right| > \texttt{clip_value}
    \end{cases}\\]

    ```plot-activation
    quantizers.SteTern
    ```

    # Arguments
    threshold_value: The value for the threshold, \\(\Delta\\).
    ternary_weight_networks: Boolean of whether to use the
        Ternary Weight Networks threshold calculation.
    clip_value: Threshold for clipping gradients. If `None` gradients are not clipped.
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # References
    - [Ternary Weight Networks](http://arxiv.org/abs/1605.04711)
    """

    precision = 2

    def __init__(
        self,
        threshold_value: float = 0.05,
        ternary_weight_networks: bool = False,
        clip_value: float = 1.0,
        **kwargs,
    ):
        self.threshold_value = threshold_value
        self.ternary_weight_networks = ternary_weight_networks
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        super().call(inputs)
        return ste_tern(
            inputs,
            threshold_value=self.threshold_value,
            ternary_weight_networks=self.ternary_weight_networks,
            clip_value=self.clip_value,
        )

    def get_config(self):
        return {
            **super().get_config(),
            "threshold_value": self.threshold_value,
            "ternary_weight_networks": self.ternary_weight_networks,
            "clip_value": self.clip_value,
        }


@utils.register_alias("dorefa_quantizer")
@utils.register_keras_custom_object
class DoReFaQuantizer(BaseQuantizer):
    r"""Instantiates a serializable k_bit quantizer as in the DoReFa paper.

    \\[
    q(x) = \begin{cases}
    0 & x < \frac{1}{2n} \\\
    \frac{i}{n} & \frac{2i-1}{2n} < x < \frac{2i+1}{2n} \text{ for } i \in \\{1,n-1\\}\\\
     1 & \frac{2n-1}{2n} < x
    \end{cases}
    \\]

    where \\(n = 2^{\text{k_bit}} - 1\\). The number of bits, k_bit, needs to be passed as an argument.
    The gradient is estimated using the Straight-Through Estimator
    (essentially the binarization is replaced by a clipped identity on the
    backward pass).
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
    1 &  0 \leq x \leq 1 \\\
    0 & \text{else}
    \end{cases}\\]

    ```plot-activation
    quantizers.DoReFaQuantizer
    ```

    # Arguments
    k_bit: number of bits for the quantization.
    metrics: An array of metrics to add to the layer. If `None` the metrics set in
        `larq.metrics.scope` are used. Currently only the `flip_ratio` metric is
        available.

    # Returns
    Quantization function

    # References
    - [DoReFa-Net: Training Low Bitwidth Convolutional Neural Networks
      with Low Bitwidth Gradients](https://arxiv.org/abs/1606.06160)
    """
    precision = None

    def __init__(self, k_bit: int = 2, **kwargs):
        self.precision = k_bit
        super().__init__(**kwargs)

    def call(self, inputs):
        super().call(inputs)
        inputs = tf.clip_by_value(inputs, 0.0, 1.0)

        @tf.custom_gradient
        def _k_bit_with_identity_grad(x):
            n = 2 ** self.precision - 1
            return tf.round(x * n) / n, lambda dy: dy

        return _k_bit_with_identity_grad(inputs)

    def get_config(self):
        return {**super().get_config(), "k_bit": self.precision}


Quantizer = Union[tf.keras.layers.Layer, Callable[[tf.Tensor], tf.Tensor]]


def serialize(quantizer: tf.keras.layers.Layer):
    return tf.keras.utils.serialize_keras_object(quantizer)


def deserialize(name, custom_objects=None):
    return tf.keras.utils.deserialize_keras_object(
        name,
        module_objects=globals(),
        custom_objects=custom_objects,
        printable_module_name="quantization function",
    )


def get(identifier):
    if identifier is None:
        return None
    if isinstance(identifier, dict):
        return deserialize(identifier)
    if isinstance(identifier, str):
        return deserialize(str(identifier))
    if callable(identifier):
        return identifier
    raise ValueError(
        f"Could not interpret quantization function identifier: {identifier}"
    )
