"""We add metrics specific to extremely quantized networks using a `scope` rather than
through the `metrics` parameter of `model.compile()`, where most common metrics reside.
This is because, to calculate metrics like the `flip_ratio`, we need a layer's kernel or
activation and not just the `y_true` and `y_pred` that Keras passes to metrics defined
in the usual way.
"""

from contextlib import contextmanager

import numpy as np
import tensorflow as tf

from larq import utils

__all__ = ["scope", "get_training_metrics"]

_GLOBAL_TRAINING_METRICS = set()
_AVAILABLE_METRICS = {"flip_ratio"}


@contextmanager
def scope(metrics=[]):
    """A context manager to set the training metrics to be used in quantizers.

    !!! example
        ```python
        with larq.metrics.scope(["flip_ratio"]):
            model = tf.keras.models.Sequential(
                [larq.layers.QuantDense(3, kernel_quantizer="ste_sign", input_shape=(32,))]
            )
        model.compile(loss="mse", optimizer="sgd")
        ```

    # Arguments
    metrics: Iterable of metrics to add to quantizers defined inside this context.
        Currently only the `flip_ratio` metric is available.
    """
    for metric in metrics:
        if metric not in _AVAILABLE_METRICS:
            raise ValueError(
                f"Unknown training metric '{metric}'. Available metrics: {_AVAILABLE_METRICS}."
            )
    backup = _GLOBAL_TRAINING_METRICS.copy()
    _GLOBAL_TRAINING_METRICS.update(metrics)
    yield _GLOBAL_TRAINING_METRICS
    _GLOBAL_TRAINING_METRICS.clear()
    _GLOBAL_TRAINING_METRICS.update(backup)


def get_training_metrics():
    """Retrieves a live reference to the training metrics in the current scope.

    Updating and clearing training metrics using `larq.metrics.scope` is preferred,
    but `get_training_metrics` can be used to directly access them.

    !!! example
        ```python
        get_training_metrics().clear()
        get_training_metrics().add("flip_ratio")
        ```

    # Returns
    A set of training metrics in the current scope.
    """
    return _GLOBAL_TRAINING_METRICS


@utils.register_alias("flip_ratio")
@utils.register_keras_custom_object
class FlipRatio(tf.keras.metrics.Metric):
    """Computes the mean ration of changed values in a given tensor.

    !!! example
        ```python
        m = metrics.FlipRatio(values_shape=(2,))
        m.update_state((1, 1))  # result: 0
        m.update_state((2, 2))  # result: 1
        m.update_state((1, 2))  # result: 0.75
        print('Final result: ', m.result().numpy())  # Final result: 0.75
        ```

    # Arguments
    name: Name of the metric.
    values_dtype: Data type of the tensor for which to track changes.
    dtype: Data type of the moving mean.
    """

    def __init__(self, name="flip_ratio", values_dtype="int8", dtype=None):
        super().__init__(name=name, dtype=dtype)

        self.values_dtype = tf.as_dtype(values_dtype)
        self.values_shape = None
        self._built = False

    def _build(self, shape):
        self.values_shape = shape

        self.is_weight_metric = True
        self._size = np.prod(self.values_shape)
        # Fails here because of None in shape

        with tf.init_scope():
            self._previous_values = self.add_weight(
                "previous_values",
                shape=self.values_shape,
                dtype=self.values_dtype,
                initializer=tf.keras.initializers.zeros,
                aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
            )
            self.total = self.add_weight(
                "total",
                initializer=tf.keras.initializers.zeros,
                aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
            )
            self.count = self.add_weight(
                "count",
                initializer=tf.keras.initializers.zeros,
                aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
            )

        # TODO: also initialize variables with keras backend?
        # https://github.com/tensorflow/tensorflow/blob/944e6fe82a2b7733dd2f58ad352fcaeb7ad066b8/tensorflow/python/keras/metrics.py#L2723
        # https://github.com/tensorflow/tensorflow/blob/944e6fe82a2b7733dd2f58ad352fcaeb7ad066b8/tensorflow/python/keras/backend.py#L955

        self._built = True

    def update_state(self, values, sample_weight=None):
        if not self._built:
            print(values.shape)
            # This still has shape (None, 2) for `pytest -k "layers_test" -s -x`, while
            # we'd expect it to have the actual shape I think.
            self._build(values.shape)

        values = tf.cast(values, self.values_dtype)
        unchanged_values = tf.math.count_nonzero(
            tf.equal(self._previous_values, values)
        )
        flip_ratio = 1 - (tf.cast(unchanged_values, self.dtype) / self._size)

        update_total_op = self.total.assign_add(flip_ratio * tf.sign(self.count))
        with tf.control_dependencies([update_total_op]):
            update_count_op = self.count.assign_add(1)
            with tf.control_dependencies([update_count_op]):
                return self._previous_values.assign(values)

    def result(self):
        return tf.compat.v1.div_no_nan(self.total, self.count - 1)

    def reset_states(self):
        tf.keras.backend.batch_set_value(
            [(v, 0) for v in self.variables if v is not self._previous_values]
        )

    def get_config(self):
        return {
            **super().get_config(),
            "values_shape": self.values_shape,
            "values_dtype": self.values_dtype.name,
        }
