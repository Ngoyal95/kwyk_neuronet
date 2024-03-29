"""MeshNet implemented in TensorFlow.

Reference
---------
Fedorov, A., Johnson, J., Damaraju, E., Ozerin, A., Calhoun, V., & Plis, S.
(2017, May). End-to-end learning of brain tissue segmentation from imperfect
labeling. IJCNN 2017. (pp. 3785-3792). IEEE.
"""

import numpy as np
import tensorflow as tf
from tensorflow.contrib.estimator import TowerOptimizer, replicate_model_fn
from tensorflow.python.estimator.canned.optimizers import (
    get_optimizer_instance
)

from nobrainer.models.util import check_required_params, set_default_params, str2bool
from nobrainer.models import vwn_conv
from nobrainer.models.bayesian_dropout import concrete_dropout

def _layer(inputs,
           mode,
           layer_num,
           filters,
           kernel_size,
           dilation_rate,
           is_mc_b,
           is_mc_g,
          use_expectation):
    """Layer building block of MeshNet.

    Performs 3D convolution, activation, batch normalization, and dropout on
    `inputs` tensor.

    Args:
        inputs : float `Tensor`, input tensor.
        mode : string, a TensorFlow mode key.
        layer_num : int, value to append to each operator name. This should be
            the layer number in the network.
        filters : int, number of 3D convolution filters.
        kernel_size : int or tuple, size of 3D convolution kernel.
        dilation_rate : int or tuple, rate of dilution in 3D convolution.
        dropout_rate : float, the dropout rate between 0 and 1.

    Returns:
        `Tensor` of same type as `inputs`.
    """
    training = mode == tf.estimator.ModeKeys.TRAIN

    with tf.variable_scope('layer_{}'.format(layer_num)):
        conv = vwn_conv.conv3d(
            inputs, filters=filters, kernel_size=kernel_size,
            padding='SAME', dilation_rate=dilation_rate, activation=None,is_mc=is_mc_g
        )
        conv = concrete_dropout(conv,is_mc_b,filters,use_expectation=use_expectation)
        return tf.nn.relu(conv)


def model_fn(features,
             labels,
             mode,
             params):
    """MeshNet model function.

    Args:
        features: 5D float `Tensor`, input tensor. This is the first item
            returned from the `input_fn` passed to `train`, `evaluate`, and
            `predict`. Use `NDHWC` format.
        labels: 4D float `Tensor`, labels tensor. This is the second item
            returned from the `input_fn` passed to `train`, `evaluate`, and
            `predict`. Labels should not be one-hot encoded.
        mode: Optional. Specifies if this training, evaluation or prediction.
        params: `dict` of parameters. All parameters below are required.
            - n_classes: number of classes to classify.
            - optimizer: instance of TensorFlow optimizer.
            - n_filters: number of filters to use in each convolution. The
                original implementation used 21 filters to classify brainmask
                and 71 filters for the multi-class problem.
            - dropout_rate: rate of dropout. For example, 0.1 would drop 10% of
                input units.

    Returns:
        `tf.estimator.EstimatorSpec`

    Raises:
        `ValueError` if required parameters are not in `params`.
    """
    volume = features
    if isinstance(volume, dict):
        volume = features['volume']
    
    required_keys = {'n_classes'}
    default_params = {'optimizer': None,'n_filters': 96}
    check_required_params(params=params, required_keys=required_keys)
    set_default_params(params=params, defaults=default_params)

    tf.logging.debug("Parameters for model:")
    tf.logging.debug(params)

    # Dilation rate by layer.
    dilation_rates = (
        (1, 1, 1),
        (1, 1, 1),
        (1, 1, 1),
        (2, 2, 2),
        (4, 4, 4),
        (8, 8, 8),
        (1, 1, 1),
    )
    
    is_mc_g = tf.constant(str2bool('False'),dtype=tf.bool)
    is_mc_b = tf.constant(str2bool(params['is_mc']),dtype=tf.bool)
    
    outputs = volume
    
    for ii, dilation_rate in enumerate(dilation_rates):
        outputs = _layer(
            outputs, mode=mode, layer_num=ii + 1, filters=params['n_filters'],
            kernel_size=3, dilation_rate=dilation_rate, is_mc_b=is_mc_b, is_mc_g=is_mc_g, use_expectation=str2bool(params['is_mc'])
        )

    with tf.variable_scope('logits'):
        logits = vwn_conv.conv3d(
            inputs=outputs, filters=params['n_classes'], kernel_size=(1, 1, 1),
            padding='SAME', activation=None, is_mc=is_mc_g
        )

    predicted_classes = tf.argmax(logits, axis=-1)

    if mode == tf.estimator.ModeKeys.PREDICT:
        predictions = {
            'class_ids': predicted_classes,
            'probabilities': tf.nn.softmax(logits),
            'logits': logits,
        }
        export_outputs = {
            'outputs': tf.estimator.export.PredictOutput(predictions)}
        return tf.estimator.EstimatorSpec(
            mode=mode,
            predictions=predictions,
            export_outputs=export_outputs)

    if params['prior_path'] != None:
        prior_np = np.load(params['prior_path'])
    
    with tf.variable_scope("prior"):
        i=-1
        for v in tf.get_collection('ms'):
            i += 1
            if params['prior_path'] == None:
                tf.add_to_collection('ms_prior',tf.Variable(tf.constant(0, dtype = v.dtype, shape = v.shape),trainable = False))
            else:
                tf.add_to_collection('ms_prior',tf.Variable(tf.convert_to_tensor(prior_np[0][i], dtype = tf.float32),trainable = False))
        
        ms = tf.get_collection('ms')
        ms_prior = tf.get_collection('ms_prior')

        print(len(ms))
        i=-1
        for v in tf.get_collection('ms'):
            i += 1
            if params['prior_path'] == None:
                tf.add_to_collection('sigmas_prior',tf.Variable(tf.constant(0.1, dtype = v.dtype, shape = v.shape),trainable = False))
            else:
                tf.add_to_collection('sigmas_prior',tf.Variable(tf.convert_to_tensor(prior_np[1][i], dtype = tf.float32),trainable = False))

        sigmas = tf.get_collection('sigmas')
        sigmas_prior = tf.get_collection('sigmas_prior')
     
    nll_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=labels, logits=logits))
    tf.summary.scalar('nll_loss', nll_loss)
    
    l2_loss = tf.add_n([tf.reduce_sum((tf.square(ms[i] - ms_prior[i])) / ((tf.square(sigmas_prior[i]) + 1e-8) * 2.0)) for i in range(len(ms))], name = 'l2_loss')
    tf.summary.scalar('l2_loss', l2_loss)
    
    
    sigma_squared_loss = tf.add_n([tf.reduce_sum(tf.square(sigmas[i]) / ((tf.square(sigmas_prior[i]) + 1e-8) * 2.0)) for i in range(len(sigmas))],name = 'sigma_squared_loss')
    tf.summary.scalar('sigma_squared_loss', sigma_squared_loss)
    
    log_sigma_loss = tf.add_n([tf.reduce_sum(tf.log(v+1e-8)) for v in sigmas],name='log_sigmas_loss')
    tf.summary.scalar('log_sigma_loss', log_sigma_loss)
    
    ps = tf.get_collection('ps')
    p_prior= 0.5
    b_kld_loss = tf.add_n([tf.reduce_sum(p * (tf.log(p+1e-7) - tf.log(p_prior)) + (1.0 - p) * (tf.log(1.0-p+1e-7) - tf.log(1.0-p_prior))) for p in ps],name='b_kld_loss')
    tf.summary.scalar('b_kld_loss', b_kld_loss)
    
    n_examples = tf.constant(params['n_examples'],dtype=ms[0].dtype)
    tf.summary.scalar('n_examples', n_examples)
    
    
    if not params['only_kld']:
        
        if str2bool(params['is_mc']):
            loss = nll_loss + (l2_loss + sigma_squared_loss - log_sigma_loss + b_kld_loss) / n_examples
        else:
            loss = nll_loss + (l2_loss) / n_examples
    
    else:
        mse_m_loss = tf.add_n([tf.reduce_sum(tf.square(ms[i] - ms_prior[i])) for i in range(len(ms))], name = 'mse_m_loss')
        tf.summary.scalar('mse_m_loss', l2_loss)
    
        mse_sigmas_loss = tf.add_n([tf.reduce_sum(tf.square(sigmas[i] - sigmas_prior[i])) for i in range(len(sigmas))], name = 'mse_sigmas_loss')
        tf.summary.scalar('mse_sigmas_loss',  mse_sigmas_loss)
        loss = mse_m_loss + mse_sigmas_loss
    
    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(
            mode=mode, loss=loss, eval_metric_ops=None,
        )

    assert mode == tf.estimator.ModeKeys.TRAIN

    global_step = tf.train.get_global_step()
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
        train_op = params['optimizer'].minimize(loss, global_step=global_step)
    return tf.estimator.EstimatorSpec(mode=mode, loss=loss, train_op=train_op)


class MeshNetCWN(tf.estimator.Estimator):
    """MeshNet model.

    Example:
        ```python
        import numpy as np
        import tensorflow as tf

        shape = (1, 10, 10, 10)  # Batch of 1.
        X = np.random.rand(*shape, 1).astype(np.float32)
        y = np.random.randint(0, 9, size=(shape), dtype=np.int32)
        dset_fn = lambda: tf.data.Dataset.from_tensors((X, y))
        estimator = nobrainer.models.MeshNet(
            n_classes=10, optimizer='Adam', n_filters=71, dropout_rate=0.25,
            learning_rate=0.001,
        )
        estimator.train(input_fn=dset_fn)
        ```

    Args:
        n_classes: int, number of classes to classify.
        optimizer: instance of TensorFlow optimizer or string of optimizer
            name.
        n_filters: int (default 21), number of filters to use in each
            convolution. The original implementation used 21 filters to
            classify brainmask and 71 filters for the multi-class problem.
        dropout_rate: float in range [0, 1] (default 0.25). Rate of dropout.For
            example, 0.1 would drop 10% of input units.
        learning_rate: float, only required if `optimizer` is a string.
        model_dir: Directory to save model parameters, graph, etc. This can
            also be used to load checkpoints from the directory in an estimator
            to continue training a previously saved model. If PathLike object,
            the path will be resolved. If None, the model_dir in config will be
            used if set. If both are set, they must be same. If both are None,
            a temporary directory will be used.
        config: Configuration object.
        warm_start_from: Optional string filepath to a checkpoint to warm-start
            from, or a `tf.estimator.WarmStartSettings` object to fully
            configure warm-starting. If the string filepath is provided instead
            of a `WarmStartSettings`, then all variables are warm-started, and
            it is assumed that vocabularies and Tensor names are unchanged.
        multi_gpu: boolean, if true, optimizer is wrapped in
            `tf.contrib.estimator.TowerOptimizer` and model function is wrapped
            in `tf.contrib.estimator.replicate_model_fn()`.
    """
    def __init__(self,
                 n_classes,
                 optimizer,
                 n_filters=64,
                 n_examples=1.0,
                 n_prior_samples=1.0,
                 learning_rate=None,
                 model_dir=None,
                 config=None,
                 warm_start_from=None,
                 prior_path=None,
                 multi_gpu=False,
                 only_kld=False,
                 is_mc='True'):
        print('Learning Rate: ' + str(learning_rate))
        params = {
            'n_classes': n_classes,
            # If an instance of an optimizer is passed in, this will just
            # return it.
            'optimizer': get_optimizer_instance(optimizer, learning_rate),
            'n_filters': n_filters,
            'n_examples': n_examples,
            'prior_path': prior_path,
            'n_prior_samples': n_prior_samples,
            'only_kld': only_kld,
            'is_mc': is_mc
        }

        _model_fn = model_fn

        if multi_gpu:
            params['optimizer'] = TowerOptimizer(params['optimizer'])
            _model_fn = replicate_model_fn(_model_fn)

        super(MeshNetCWN, self).__init__(
            model_fn=_model_fn, model_dir=model_dir, params=params,
            config=config, warm_start_from=warm_start_from
        )