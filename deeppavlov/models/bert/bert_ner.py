# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import List, Any

import tensorflow as tf
from tensorflow.python.ops import array_ops
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.component import Component
from deeppavlov.core.models.lr_scheduled_tf_model import LRScheduledTFModel
from deeppavlov.core.commands.utils import expand_path
from logging import getLogger

from bert_dp.modeling import BertConfig, BertModel
from bert_dp.optimization import AdamWeightDecayOptimizer

logger = getLogger(__name__)


@register('bert_ner')
class BertNerModel(LRScheduledTFModel):
    # TODO: docs
    # TODO: add warmup
    # TODO: add head-only pre-training
    def __init__(self,
                 bert_config_file: str,
                 n_tags: List[str],
                 keep_prob: float,
                 attention_probs_keep_prob: float = None,
                 hidden_keep_prob: float = None,
                 encoder_layer_ids: List[int] = list(range(12)),
                 optimizer: str = None,
                 num_warmup_steps: int = None,
                 focal_alpha: float = None,
                 focal_gamma: float = None,
                 weight_decay_rate: float = 0.01,
                 return_probas: bool = False,
                 pretrained_bert: str = None,
                 min_learning_rate: float = 1e-06,
                 **kwargs) -> None:
        super().__init__(**kwargs)

        self.return_probas = return_probas
        self.n_tags = n_tags
        self.min_learning_rate = min_learning_rate
        self.keep_prob = keep_prob
        self.optimizer = optimizer
        self.focal_alpha = focal_alpha
        self.focal_gamma = focal_gamma
        self.encoder_layer_ids = encoder_layer_ids
        self.num_warmup_steps = num_warmup_steps
        self.weight_decay_rate = weight_decay_rate

        self.bert_config = BertConfig.from_json_file(str(expand_path(bert_config_file)))

        if attention_probs_keep_prob is not None:
            self.bert_config.attention_probs_dropout_prob = 1.0 - attention_probs_keep_prob
        if hidden_keep_prob is not None:
            self.bert_config.hidden_dropout_prob = 1.0 - hidden_keep_prob

        self.sess_config = tf.ConfigProto(allow_soft_placement=True)
        self.sess_config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=self.sess_config)

        self._init_graph()

        self._init_optimizer()
        for var in tf.global_variables():
            print('{:<31}'.format(repr(var.get_shape().as_list())), var.name)

        self.sess.run(tf.global_variables_initializer())

        if pretrained_bert is not None:
            pretrained_bert = str(expand_path(pretrained_bert))

        # print(tf.train.checkpoint_exists(pretrained_bert))
        # print(tf.train.checkpoint_exists(str(self.load_path.resolve())))
        if tf.train.checkpoint_exists(pretrained_bert) \
                and not tf.train.checkpoint_exists(str(self.load_path.resolve())):
            logger.info('[initializing model with Bert from {}]'.format(pretrained_bert))
            # Exclude optimizer and classification variables from saved variables
            var_list = self._get_saveable_variables(
                exclude_scopes=('Optimizer', 'learning_rate', 'momentum', 'ner'))
            saver = tf.train.Saver(var_list)
            saver.restore(self.sess, pretrained_bert)
        # print(self.sess.run(tf.trainable_variables()[-3]))

        if self.load_path is not None:
            self.load()

    def _init_graph(self):
        self._init_placeholders()

        self.bert = BertModel(config=self.bert_config,
                              is_training=self.is_train_ph,
                              input_ids=self.input_ids_ph,
                              input_mask=self.input_masks_ph,
                              token_type_ids=self.token_types_ph,
                              use_one_hot_embeddings=False)

        encoder_layers = [self.bert.all_encoder_layers[i]
                          for i in self.encoder_layer_ids]

        with tf.variable_scope('ner'):
            output_layer = sum(encoder_layers) / len(encoder_layers)
            output_layer = tf.nn.dropout(output_layer, keep_prob=self.keep_prob_ph)

            logits = tf.layers.dense(output_layer, units=self.n_tags, name="output_dense")

            self.y_predictions = tf.argmax(logits, -1)
            self.y_probas = tf.nn.softmax(logits)

        with tf.variable_scope("loss"):
            # NOTE: same mask as for inputs?
            y_mask = tf.cast(self.input_masks_ph, tf.float32)
            if (self.focal_alpha is None) or (self.focal_gamma is None):
                self.loss = tf.losses.sparse_softmax_cross_entropy(labels=self.y_ph,
                                                                   logits=logits,
                                                                   weights=y_mask)
            else:
                y_onehot = tf.one_hot(self.y_ph, self.n_tags)
                self.loss = self.focal_loss2(labels=y_onehot,
                                             probs=self.y_probas,
                                             weights=y_mask,
                                             alpha=self.focal_alpha,
                                             gamma=self.focal_gamma)

    @staticmethod
    def focal_loss(labels, probs, weights=None, gamma=2.0, alpha=4.0):
        """
	focal loss for multi-classification
	FL(p_t)=-alpha(1-p_t)^{gamma}ln(p_t)
	gradient is d(Fl)/d(p_t) not d(Fl)/d(x) as described in paper
	d(Fl)/d(p_t) * [p_t(1-p_t)] = d(Fl)/d(x)
	Lin, T.-Y., Goyal, P., Girshick, R., He, K., & Dollár, P. (2017).
	Focal Loss for Dense Object Detection, 130(4), 485–491.
	https://doi.org/10.1016/j.ajodo.2005.02.022
	:param labels: ground truth one-hot encoded labels,
            shape of [batch_size, num_cls]
	:param probs: model's output, shape of [batch_size, num_cls]
	:param gamma:
	:param alpha:
	:return: shape of [batch_size]
	"""
        epsilon = 1e-9
        labels = tf.cast(labels, tf.float32)
        probs = tf.cast(probs, tf.float32)
        
        probs = tf.clip_by_value(probs, epsilon, 1.0 - epsilon)
        ce = tf.multiply(labels, -tf.log(probs))
        weight = tf.multiply(labels, tf.pow(tf.subtract(1., probs), gamma))
        fl = tf.multiply(alpha, tf.multiply(weight, ce))
        if weights is not None:
            fl = tf.multiply(tf.reduce_sum(fl, axis=2), weights)
        return tf.reduce_sum(fl)

    @staticmethod
    def focal_loss2(labels, probs, weights=None, alpha=1.0, gamma=1):
        r"""Compute focal loss for predictions.
            Multi-labels Focal loss formula:
                FL = -alpha * (z-p)^gamma * log(p) -(1-alpha) * p^gamma * log(1-p)
                     ,which alpha = 0.25, gamma = 2, p = sigmoid(x), z = target_tensor.
        Args:
         labels: A float tensor of shape [batch_size, num_anchors,
            num_classes] representing the predicted logits for each class
         probs: A float tensor of shape [batch_size, num_anchors,
            num_classes] representing one-hot encoded classification targets
         weights: A float tensor of shape [batch_size, num_anchors]
         alpha: A scalar tensor for focal loss alpha hyper-parameter
         gamma: A scalar tensor for focal loss gamma hyper-parameter
        Returns:
            loss: A (scalar) tensor representing the value of the loss function
        """
        labels = tf.cast(labels, tf.float32)
        probs = tf.cast(probs, tf.float32)

        zeros = array_ops.zeros_like(probs, dtype=probs.dtype)
        
        # For positive prediction, only need consider front part loss, back part is 0;
        # target_tensor > zeros <=> z=1, so poitive coefficient = z - p.
        pos_p_sub = array_ops.where(labels > zeros, labels - probs, zeros)
        
        # For negative prediction, only need consider back part loss, front part is 0;
        # target_tensor > zeros <=> z=1, so negative coefficient = 0.
        neg_p_sub = array_ops.where(labels > zeros, zeros, probs)
        per_entry_cross_ent = - alpha * (pos_p_sub ** gamma) *\
                tf.log(tf.clip_by_value(probs, 1e-8, 1.0)) \
                - (1 - alpha) * (neg_p_sub ** gamma) * \
                tf.log(tf.clip_by_value(1.0 - probs, 1e-8, 1.0))
        if weights is not None:
            per_entry_cross_ent = tf.multiply(per_entry_cross_ent,
                    tf.expand_dims(weights, -1))
        return tf.reduce_sum(per_entry_cross_ent)

    def _init_placeholders(self):
        self.input_ids_ph = tf.placeholder(shape=(None, None),
                                           dtype=tf.int32,
                                           name='token_indices_ph')
        self.input_masks_ph = tf.placeholder(shape=(None, None),
                                             dtype=tf.int32,
                                             name='token_mask_ph')
        self.token_types_ph = tf.placeholder(shape=(None, None),
                                             dtype=tf.int32,
                                             name='token_types_ph')

        self.y_ph = tf.placeholder(shape=(None, None), dtype=tf.int32, name='y_ph')

        self.learning_rate_ph = tf.placeholder_with_default(0.0, shape=[], name='learning_rate_ph')
        self.keep_prob_ph = tf.placeholder_with_default(1.0, shape=[], name='keep_prob_ph')
        self.is_train_ph = tf.placeholder_with_default(False, shape=[], name='is_train_ph')

    def _init_optimizer(self):
        with tf.variable_scope('Optimizer'):
            self.global_step = tf.get_variable('global_step', shape=[], dtype=tf.int32,
                                               initializer=tf.constant_initializer(0),
                                               trainable=False)
            # default optimizer for Bert is Adam with fixed L2 regularization
        if self.optimizer is None:
            self.train_op = \
                self.get_train_op(self.loss,
                                  learning_rate=self.learning_rate_ph,
                                  optimizer=AdamWeightDecayOptimizer,
                                  weight_decay_rate=self.weight_decay_rate,
                                  beta_1=0.9,
                                  beta_2=0.999,
                                  epsilon=1e-6,
                                  optimizer_scope_name='Optimizer',
                                  exclude_from_weight_decay=["LayerNorm",
                                                             "layer_norm",
                                                             "bias"])
        else:
            self.train_op = self.get_train_op(self.loss,
                                              optimizer_scope_name='Optimizer',
                                              learning_rate=self.learning_rate_ph)

        if self.optimizer is None:
            with tf.variable_scope('Optimizer'):
                new_global_step = self.global_step + 1
                self.train_op = tf.group(self.train_op, [self.global_step.assign(new_global_step)])

    def _build_feed_dict(self, input_ids, input_masks, token_types, y=None):
        feed_dict = {
            self.input_ids_ph: input_ids,
            self.input_masks_ph: input_masks,
            self.token_types_ph: token_types,
        }
        if y is not None:
            feed_dict.update({
                self.y_ph: y,
                self.learning_rate_ph: max(self.get_learning_rate(), self.min_learning_rate),
                self.keep_prob_ph: self.keep_prob,
                self.is_train_ph: True,
            })

        return feed_dict

    def train_on_batch(self,
                       input_ids: List[List[int]],
                       input_masks: List[List[int]],
                       y: List[List[int]]) -> dict:
        input_type_ids = [[0] * len(inputs) for inputs in input_ids]
        for ids, masks, ys in zip(input_ids, input_masks, y):
            assert len(ids) == len(masks) == len(ys), \
                f"ids({len(ids)}) = {ids}, masks({len(masks)}) = {masks},"\
                f" ys({len(ys)}) = {ys} should have the same length."

        feed_dict = self._build_feed_dict(input_ids, input_masks, input_type_ids, y)

        _, loss = self.sess.run([self.train_op, self.loss], feed_dict=feed_dict)
        return {'loss': loss, 'learning_rate': feed_dict[self.learning_rate_ph]}

    def __call__(self,
                 input_ids: List[List[int]],
                 input_masks: List[List[int]]):
        input_type_ids = [[0] * len(inputs) for inputs in input_ids]
        for ids, masks in zip(input_ids, input_masks):
            assert len(ids) == len(masks), \
                f"ids({len(ids)}) = {ids}, masks({len(masks)}) = {masks}"\
                f" should have the same length."

        feed_dict = self._build_feed_dict(input_ids, input_masks, input_type_ids)
        if not self.return_probas:
            pred = self.sess.run(self.y_predictions, feed_dict=feed_dict)
        else:
            pred = self.sess.run(self.y_probas, feed_dict=feed_dict)
        return pred

    def process_event(self, event_name: str, data) -> None:
        """
        Processes events sent by trainer. Implements learning rate decay.

        Args:
            event_name: event_name sent by trainer
            data: number of examples, epochs, metrics sent by trainer
        """
        super().process_event(event_name, data)


class MaskCutter(Component):

    def __init__(self, **kwargs):
        pass

    def __call__(self,
                 samples: List[List[Any]],
                 masks: List[List[int]]):
        samples_cut = []
        for s_list, m_list in zip(samples, masks):
            samples_cut.append([])
            for j in range(len(s_list)):
                if m_list[j]:
                    samples_cut[-1].append(s_list[j])
        return samples_cut
 
