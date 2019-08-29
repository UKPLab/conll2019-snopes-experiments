import random
from datetime import datetime
from math import ceil

import numpy as np
import tensorflow as tf
from sklearn.base import BaseEstimator, ClassifierMixin
from tqdm import tqdm

from common.util.log_helper import LogHelper

num_birnn = 2


class ESIM(BaseEstimator, ClassifierMixin):
    """
    https://arxiv.org/abs/1609.06038
    """

    def __init__(self, name=None, ckpt_path=None, trainable=False, lstm_layers=1, num_neurons=[128, 128, 32],
                 pos_weight=None, optimizer='adam', learning_rate=0.001, batch_size=128,
                 activation='relu', initializer='he', num_epoch=100, dropout_rate=None,
                 max_check_without_progress=10, show_progress=1, tensorboard_logdir=None, random_state=None,
                 vocab_size=None, n_outputs=3, shared_rnn=True, num_units=128, h_max_length=50, s_max_length=50,
                 sent_threshold=0.7, max_gpu_memory=0.5):

        self.ckpt_path = ckpt_path
        self.trainable = trainable
        self.lstm_layers = lstm_layers
        self.num_neurons = num_neurons
        self.optimizer = optimizer
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.activation = activation
        self.num_epoch = num_epoch
        self.initializer = initializer
        self.dropout_rate = dropout_rate
        self.max_check_without_progress = max_check_without_progress
        self.show_progress = show_progress
        self.random_state = random_state
        self.tensorboard_logdir = tensorboard_logdir
        self.vocab_size = vocab_size
        self.n_outputs = n_outputs
        self.pos_weight = pos_weight
        self.name = name
        self.shared_rnn = shared_rnn
        self.num_units = num_units
        self.h_max_length = h_max_length
        self.s_max_length = s_max_length
        self.sent_threshold = sent_threshold
        self.max_gpu_memory = max_gpu_memory
        self.embedding = None
        self._graph = None
        self._classes = None
        self._session = None
        self._initializer = None
        self._optimizer = None
        self._activation = None
        self.logger = LogHelper.get_logger(self.name)

    def __reduce__(self):
        """
        For pickle's usage
        :return:
        """
        return (ESIM, (
            self.name, self.ckpt_path, self.trainable, self.lstm_layers, self.num_neurons, self.pos_weight,
            self.optimizer, self.learning_rate, self.batch_size, self.activation, self.initializer, self.num_epoch,
            self.dropout_rate, self.max_check_without_progress, self.show_progress, self.tensorboard_logdir,
            self.random_state, self.vocab_size, self.n_outputs, self.shared_rnn, self.num_units, self.h_max_length,
            self.s_max_length, self.sent_threshold, self.max_gpu_memory))

    def lstm_cell(self, hidden_size):
        lstm = tf.nn.rnn_cell.BasicLSTMCell(hidden_size)
        if self.dropout_rate:
            lstm = tf.nn.rnn_cell.DropoutWrapper(lstm, input_keep_prob=self.keep_prob, output_keep_prob=self.keep_prob)
        return lstm

    def gru_cell(self, num_neuron):

        gru = tf.contrib.rnn.GRUCell(num_neuron)
        if self.dropout_rate:
            gru = tf.contrib.rnn.DropoutWrapper(gru, input_keep_prob=self.keep_prob, output_keep_prob=self.keep_prob)
        return gru

    def _bidirectional_rnn(self, inputs, inputs_length, num_units, scope=None):

        with tf.variable_scope(scope or 'birnn'):

            if self.lstm_layers == 1:
                rnn_cells_fw = self.lstm_cell(num_units)
                rnn_cells_bw = self.lstm_cell(num_units)
            else:
                rnn_cells_fw = tf.nn.rnn_cell.MultiRNNCell([self.lstm_cell(n) for n in num_units])
                rnn_cells_bw = tf.nn.rnn_cell.MultiRNNCell([self.lstm_cell(n) for n in num_units])

            ((fw_outputs, bw_outputs), (fw_states, bw_states)) = tf.nn.bidirectional_dynamic_rnn(rnn_cells_fw,
                                                                                                 rnn_cells_bw, inputs,
                                                                                                 inputs_length,
                                                                                                 dtype=tf.float32)
            outputs = tf.concat([fw_outputs, bw_outputs], axis=2)

            if self.lstm_layers > 1:
                fw_states = fw_states[self.lstm_layers - 1]
                bw_states = bw_states[self.lstm_layers - 1]
        return outputs, fw_states, bw_states

    def _esim(self, head_output_steps, body_output_steps, head_sent_sizes, body_sent_sizes):
        """
        Perform ESIM for each sentence pair
        :param head_output_steps: Output time steps of BiLSTM for one claim sentence but with batch size. batch_size * sents * words * embeddings
        :param body_output_steps: Output time steps of BiLSTM for one evidence sentence but with batch size. batch_size * sents * words * embeddings
        :param head_sent_sizes: Sentence sizes of the claim sentence but with batch size. batch_size * sents
        :param body_sent_sizes: Sentence sizes of the evidence sentence but with batch size. batch_size * sents
        :return:
        """
        with tf.variable_scope("ESIM_single_sentence_pair", reuse=True):
            # batch_size * sents * head_words * body_words
            attention_matrix = tf.matmul(head_output_steps, body_output_steps, transpose_b=True)
            # batch_size * sents * head_words * body_words
            soft_attention_matrix_for_head = tf.nn.softmax(attention_matrix, 3)
            # batch_size * sents * body_words * head_words
            soft_attention_matrix_for_body = tf.nn.softmax(tf.transpose(attention_matrix, [0, 1, 3, 2]), 3)

            # batch_size * sents * head_words * embed
            head_attended = tf.matmul(soft_attention_matrix_for_head, body_output_steps, name="attend_head")

            # batch_size * sents * body_words * embed
            body_attended = tf.matmul(soft_attention_matrix_for_body, head_output_steps, name="attend_body")

            batch_size, num_sents, max_head_words, embed_size = tf.unstack(tf.shape(head_attended))
            flat_head_sent_sizes = tf.reshape(head_sent_sizes, [batch_size * num_sents])
            flat_head_seq_masks = tf.sequence_mask(flat_head_sent_sizes, max_head_words)
            head_seq_masks = tf.reshape(flat_head_seq_masks, [batch_size, num_sents, max_head_words])
            # batch_size * sents * head_words
            head_masks_per_word = tf.where(head_seq_masks, tf.ones_like(head_seq_masks, dtype=tf.float32),
                                           tf.zeros_like(head_seq_masks, dtype=tf.float32))
            encode_size = 2 * self.num_neurons[0]
            # now batch_size * sents * head_words * embed
            head_masks = tf.tile(tf.expand_dims(head_masks_per_word, 3), [1, 1, 1, encode_size])
            masked_head_attended = head_attended * head_masks

            _, _, max_body_words, _ = tf.unstack(tf.shape(body_attended))
            flat_body_sent_sizes = tf.reshape(body_sent_sizes, [batch_size * num_sents])
            flat_body_seq_masks = tf.sequence_mask(flat_body_sent_sizes, max_body_words)
            body_seq_masks = tf.reshape(flat_body_seq_masks, [batch_size, num_sents, max_body_words])
            # batch_size * sents * body_words
            body_masks_per_word = tf.where(body_seq_masks, tf.ones_like(body_seq_masks, dtype=tf.float32),
                                           tf.zeros_like(body_seq_masks, dtype=tf.float32))
            # now batch_size * sents * body_words * embed
            body_masks = tf.tile(tf.expand_dims(body_masks_per_word, 3), [1, 1, 1, encode_size])
            masked_body_attended = body_attended * body_masks

            # batch_size * sents * head_words * (4 * embed)
            head_concat = tf.concat(
                [head_output_steps, masked_head_attended, tf.abs(tf.subtract(masked_head_attended, head_output_steps)),
                 tf.multiply(head_output_steps, masked_head_attended)], 3, name="concat_head_attended")
            # batch_size * sents * body_words * (4 * embed)
            body_concat = tf.concat(
                [body_output_steps, masked_body_attended, tf.abs(tf.subtract(masked_body_attended, body_output_steps)),
                 tf.multiply(body_output_steps, masked_body_attended)], 3, name="concat_body_attended")
        return head_concat, body_concat, head_masks_per_word, body_masks_per_word

    def _scores_alignment(self, batch_size, sents_size):
        return tf.reshape(self._X_scores, shape=[batch_size, sents_size, 1])

    def _trainable_alignment(self, sum_heads_encoded, sum_bodies_encoded, encode_size, projection_size=100):
        """
        Generate the alignments over the evidences by calculating the cosine similarity of the sum of encoded words of each claim-evidence pair
        :param sum_heads_encoded: batch_size * encoded_size
        :param sum_bodies_encoded: batch_size * sents * encoded_size
        :param encode_size: encode size, last rank of shape
        :return: batch_size * sents * 1
        """
        with tf.variable_scope("trainable_alignment") as scope:
            batch_size, sents_size, _ = tf.unstack(tf.shape(sum_bodies_encoded))
            flat_sum_bodies_encoded = tf.reshape(sum_bodies_encoded, [batch_size * sents_size, encode_size])

            def _projection(inputs, num_units, reuse=None):
                return tf.contrib.layers.fully_connected(inputs, num_units, reuse=reuse, scope=scope)

            heads_projection = _projection(sum_heads_encoded, projection_size)
            flat_bodies_projection = _projection(flat_sum_bodies_encoded, projection_size, True)
            heads_projection = tf.expand_dims(heads_projection, 1)
            bodies_projection = tf.reshape(flat_bodies_projection, [batch_size, sents_size, projection_size])
            vector_attn = tf.reduce_sum(
                tf.multiply(tf.nn.l2_normalize(bodies_projection, 2), tf.nn.l2_normalize(heads_projection, 2)), axis=2,
                keepdims=True)
        return vector_attn

    def _align_without_softmax(self, sum_heads_encoded, sum_bodies_encoded):
        """
        Generate the alignments over the evidences by calculating the cosine similarity of the sum of encoded words of each claim-evidence pair
        :param sum_heads_encoded: batch_size * encoded_size
        :param sum_bodies_encoded: batch_size * sents * encoded_size
        :return: batch_size * sents * 1
        """
        _tmp_heads_encoded = tf.expand_dims(sum_heads_encoded, 1)
        vector_attn = tf.reduce_sum(
            tf.multiply(tf.nn.l2_normalize(sum_bodies_encoded, 2), tf.nn.l2_normalize(_tmp_heads_encoded, 2)), axis=2,
            keepdims=True)
        return vector_attn

    def _align(self, sum_heads_encoded, sum_bodies_encoded):
        """
        Generate the alignments over the evidences by calculating the cosine similarity of the sum of encoded words of each claim-evidence pair
        :param sum_heads_encoded: batch_size * encoded_size
        :param sum_bodies_encoded: batch_size * sents * encoded_size
        :return: batch_size * sents * 1
        """
        _tmp_heads_encoded = tf.expand_dims(sum_heads_encoded, 1)
        vector_attn = tf.reduce_sum(
            tf.multiply(tf.nn.l2_normalize(sum_bodies_encoded, 2), tf.nn.l2_normalize(_tmp_heads_encoded, 2)), axis=2,
            keepdims=True)
        return tf.nn.softmax(vector_attn, axis=1)

    def _multi_rnn(self, heads_embeddings, bodies_embeddings, h_sent_sizes, b_sent_sizes, scope=None, epsilon=1e-12):
        """
        :param heads_embeddings: batch_size * h_words * embed
        :param bodies_embeddings: batch_size * sents * b_words * embed
        :param h_sent_sizes: batch_size * 1
        :param b_sent_sizes: batch_size * sents
        :param scope:
        :param epsilon: avoid dividing zero
        :return:
        """
        with tf.variable_scope(scope or "ESIM_rnn"):
            with tf.variable_scope("local_inference_modelling") as scope:
                (batch_size, body_sent_size, body_word_size, embed_size) = tf.unstack(tf.shape(bodies_embeddings))
                flat_bodies_embeddings = tf.reshape(bodies_embeddings,
                                                    [batch_size * body_sent_size, body_word_size, self.embedding_size])
                flat_b_sent_sizes = tf.reshape(b_sent_sizes, [batch_size * body_sent_size], name="flat_b_sent_sizes")
                flat_bodies_encoded, _, _ = self._bidirectional_rnn(flat_bodies_embeddings, flat_b_sent_sizes,
                                                                    self.num_neurons[0])
                encode_size = 2 * self.num_neurons[0]
                bodies_encoded = tf.reshape(flat_bodies_encoded,
                                            (batch_size, body_sent_size, body_word_size, encode_size),
                                            name="bodies_encoded")
                # now batch_size * h_words * embed
                flat_heads_embeddings = heads_embeddings
                # now batch_size
                flat_h_sent_sizes = tf.squeeze(h_sent_sizes, 1, name="flat_h_sent_sizes")
                scope.reuse_variables()
                # batch_size * h_words * encode_size
                heads_encoded, _, _ = self._bidirectional_rnn(flat_heads_embeddings, flat_h_sent_sizes,
                                                              self.num_neurons[0])
                # batch_size * sents * h_words * encode_size
                tiled_heads_encoded = tf.tile(tf.expand_dims(heads_encoded, 1), [1, body_sent_size, 1, 1])
                # batch_size * sents
                tiled_h_sent_sizes = tf.tile(h_sent_sizes, [1, body_sent_size])

                heads_attended, bodies_attended, head_masks_per_word, body_masks_per_word = self._esim(
                    tiled_heads_encoded, bodies_encoded, tiled_h_sent_sizes, b_sent_sizes)

            with tf.variable_scope("inference_composition") as scope:
                attended_size = 4 * encode_size
                (batch_size, b_sent_size, h_word_size, _) = tf.unstack(tf.shape(heads_attended))
                (_, _, b_word_size, _) = tf.unstack(tf.shape(bodies_attended))
                reshape_expand_h_sent_sizes = tf.reshape(tf.tile(h_sent_sizes, [1, b_sent_size]),
                                                         [batch_size * b_sent_size], name="reshape_expand_h_sent_sizes")
                reshape_b_sent_sizes = tf.reshape(b_sent_sizes, [batch_size * b_sent_size], name="reshape_b_sent_sizes")
                flat_heads_attended = tf.reshape(heads_attended,
                                                 [batch_size * body_sent_size, h_word_size, attended_size],
                                                 name="flat_heads_attended")
                flat_bodies_attended = tf.reshape(bodies_attended,
                                                  [batch_size * body_sent_size, b_word_size, attended_size],
                                                  name="flat_bodies_attended")
                output_dim = 2 * self.num_neurons[1]
                # (batch_size * sents) * h_words * output_dim
                flat_heads_outputs, flat_heads_fw, flat_heads_bw = self._bidirectional_rnn(flat_heads_attended,
                                                                                           reshape_expand_h_sent_sizes,
                                                                                           self.num_neurons[1])
                scope.reuse_variables()
                # (batch_size * sents) * b_words * output_dim
                flat_bodies_outputs, flat_bodies_fw, flat_bodies_bw = self._bidirectional_rnn(flat_bodies_attended,
                                                                                              reshape_b_sent_sizes,
                                                                                              self.num_neurons[1])
                # (batch_size * sents) * 1
                epsilon_matrix = tf.fill([batch_size * b_sent_size, 1], epsilon)
                # all (batch_size * sents) * output_dim
                flat_heads_mean = tf.reduce_sum(flat_heads_outputs, 1) / tf.maximum(
                    tf.reshape(tf.cast(tiled_h_sent_sizes, tf.float32), [batch_size * b_sent_size, 1]), epsilon_matrix)
                flat_heads_length_mask = tf.sequence_mask(tf.reshape(tiled_h_sent_sizes, [batch_size * body_sent_size]),
                                                          h_word_size, dtype=tf.int32)
                flat_heads_max = tf.map_fn(lambda x: self._masked_reduce_max(x[0], x[1], -100),
                                           (flat_heads_outputs, flat_heads_length_mask), dtype=tf.float32)
                flat_bodies_mean = tf.reduce_sum(flat_bodies_outputs, 1) / tf.maximum(
                    tf.reshape(tf.cast(b_sent_sizes, tf.float32), [batch_size * b_sent_size, 1]), epsilon_matrix)
                flat_bodies_length_mask = tf.sequence_mask(flat_b_sent_sizes, b_word_size, dtype=tf.int32)
                flat_bodies_max = tf.map_fn(lambda x: self._masked_reduce_max(x[0], x[1], -100),
                                            (flat_bodies_outputs, flat_bodies_length_mask), dtype=tf.float32)
                # (batch_size * sents) * (4 * output_dim)
                flat_output_concat = tf.concat([flat_heads_mean, flat_heads_max, flat_bodies_mean, flat_bodies_max], 1)

            with tf.variable_scope("alignment"):
                # scores as attention
                alignments = self._scores_alignment(batch_size, b_sent_size)
                # (batch_size * sents) * 1
                flat_alignments = tf.reshape(alignments, [batch_size * b_sent_size, 1])
                # (batch_size * sents) * (4 * output_dim)
                flat_aligned_output_concat = flat_output_concat * flat_alignments
                # batch_size * sents * (4 * output_dim)
                output_concat = tf.reshape(flat_aligned_output_concat, [batch_size, b_sent_size, 4 * output_dim])
                # both batch_size * (4 * output_dim)
                epsilon_matrix = tf.fill([batch_size, 1], epsilon)
                output_mean = tf.reduce_sum(output_concat, 1) / tf.tile(
                    tf.maximum(tf.cast(tf.count_nonzero(alignments, 1), dtype=tf.float32), epsilon_matrix),
                    [1, 4 * output_dim])
                alignment_mask = tf.not_equal(tf.squeeze(alignments, axis=2),
                                              tf.zeros([batch_size, b_sent_size], dtype=tf.float32))
                alignment_mask = tf.cast(alignment_mask, dtype=tf.int32)
                output_max = tf.map_fn(lambda x: self._masked_reduce_max(x[0], x[1], -100),
                                       (output_concat, alignment_mask), dtype=tf.float32)
                # batch_size * (8 * output_dim)
                output_4_classifier_concat = tf.concat([output_mean, output_max], 1, name="output_4_classifier_concat")
        return output_4_classifier_concat

    def _ann(self, head_inputs, body_inputs, h_sent_sizes, b_sent_sizes):

        output = self._multi_rnn(head_inputs, body_inputs, h_sent_sizes, b_sent_sizes)

        for layer in range(len(self.num_neurons) - num_birnn * self.lstm_layers):

            if self.dropout_rate:
                output = tf.layers.dropout(output, rate=self.dropout_rate, training=self._training)
            output = tf.layers.dense(output, self.num_neurons[num_birnn * self.lstm_layers + layer],
                                     activation=self._activation, kernel_initializer=self._initializer,
                                     name="hidden{}".format(layer + 1))
        return output

    def _construct_graph(self):

        if self.random_state:
            tf.set_random_seed(self.random_state)
            np.random.seed(self.random_state)

        if self._initializer is None:
            if self.initializer == 'he':
                self._initializer = tf.contrib.layers.variance_scaling_initializer()

        if self._activation is None:
            if self.activation == 'relu':
                self._activation = tf.nn.relu

        if self._optimizer is None:
            if self.optimizer == 'adam':
                self._optimizer = tf.train.AdamOptimizer
            elif self.optimizer == 'adagrad':
                self._optimizer = tf.train.AdagradOptimizer
            elif self.optimizer == 'sgd':
                self._optimizer = tf.train.GradientDescentOptimizer

        self.embed_size = len(self.embedding[0]) if self.embedding is not None else 0
        self.embedding_size = self.embed_size

        self._X_scores, X_bodies, X_body_sent_sizes, X_heads, X_head_sent_sizes, X_body_sizes = self._construct_graph_sentence_selection()

        y_ = tf.placeholder(tf.int32, shape=[None], name="y")
        y_one_hot = tf.one_hot(y_, self.n_outputs, on_value=1.0, off_value=0.0, axis=-1, dtype=tf.float32)

        if self.dropout_rate:
            self._training = tf.placeholder_with_default(False, shape=[], name="training")
            self.keep_prob = tf.cond(self._training, lambda: tf.constant(1 - self.dropout_rate),
                                     lambda: tf.constant(1.0))
        else:
            self._training = None
        pre_output = self._ann(X_heads, X_bodies, X_head_sent_sizes, X_body_sent_sizes)
        logits = tf.layers.dense(
            pre_output, self.n_outputs, kernel_initializer=self._initializer, name="logits")
        probabilities = tf.nn.softmax(logits, name="probabilities")

        if self.pos_weight is None:
            xentropy = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=y_, logits=logits)
        else:
            xentropy = tf.nn.weighted_cross_entropy_with_logits(y_one_hot, logits, self.pos_weight)
        loss = tf.reduce_mean(xentropy, name="loss")
        variables = tf.trainable_variables()
        for v in variables:
            self.logger.debug(self.name + ": " + v.name)

        optimizer = self._optimizer(learning_rate=self.learning_rate)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            training_op = optimizer.minimize(loss)

        correct = tf.nn.in_top_k(logits, y_, 1)
        accuracy = tf.reduce_mean(tf.cast(correct, tf.float32), name="accuracy")
        _, predicts = tf.nn.top_k(logits, k=1, sorted=False)
        confusion_matrix = tf.confusion_matrix(y_, predicts, num_classes=self.n_outputs, name="confusion_matrix")

        init = tf.global_variables_initializer()
        saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES))

        if self.tensorboard_logdir:
            now = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
            tb_logdir = self.tensorboard_logdir + "/run{}".format(now)
            cost_summary = tf.summary.scalar("validation_loss", loss)
            acc_summary = tf.summary.scalar("validation_accuracy", accuracy)
            merged_summary = tf.summary.merge_all()
            file_writer = tf.summary.FileWriter(
                tb_logdir, tf.get_default_graph())

            self._merged_summary = merged_summary
            self._file_writer = file_writer

        self.y = y_
        self._logits = logits
        self._probabilities = probabilities
        self._loss = loss
        self._training_op = training_op
        self._accuracy = accuracy
        self._confusion_matrix = confusion_matrix
        self._init, self._saver = init, saver

    def _mask_3d(self, inputs, sentence_lengths, mask_value, dimension=2):

        if dimension == 1:
            inputs = tf.transpose(inputs, [0, 2, 1])

        time_steps1 = tf.shape(inputs)[1]
        time_steps2 = tf.shape(inputs)[2]

        pad_values = mask_value * tf.ones_like(inputs, dtype=tf.float32)
        mask = tf.sequence_mask(sentence_lengths, time_steps2)

        mask_3d = tf.tile(tf.expand_dims(mask, 1), (1, time_steps1, 1))
        masked = tf.where(mask_3d, inputs, pad_values)

        if dimension == 1:
            masked = tf.transpose(masked, [0, 2, 1])
        return masked

    def _atten_softmax3d(self, inputs):

        shape = tf.shape(inputs)
        num_units = shape[2]
        inputs = tf.reshape(inputs, tf.stack([-1, num_units]))
        soft_max = tf.nn.softmax(inputs)
        soft_max = tf.reshape(soft_max, shape)
        return soft_max

    def _inter_atten(self, claim, sent, claim_lengths, sent_lengths):

        with tf.variable_scope('inter-attention') as scope:
            sent = tf.transpose(sent, [0, 2, 1])
            attention = tf.matmul(claim, sent)
            att_sent1 = self._atten_softmax3d(attention)
            att_sent1 = self._mask_3d(att_sent1, sent_lengths, 0)
            att_transpose = tf.transpose(attention, [0, 2, 1])
            att_sent2 = self._atten_softmax3d(att_transpose)
            att_sent2 = self._mask_3d(att_sent2, claim_lengths, 0)
            self.att_sent2 = att_sent2

            alpha = tf.matmul(att_sent2, claim, name="alpha")
            sent = tf.transpose(sent, [0, 2, 1])
            beta = tf.matmul(att_sent1, sent, name="beta")

        return alpha, beta

    def mlp(self, outputs):

        outputs = tf.layers.dense(outputs, 256, activation=tf.nn.tanh, kernel_initializer=self._initializer)
        if self.dropout_rate:
            outputs = tf.layers.dropout(outputs, rate=self.dropout_rate, training=self._training)

        return outputs

    def _construct_graph_sentence_selection(self):
        epsilon = 1e-12
        if self.random_state:
            tf.set_random_seed(self.random_state)
            np.random.seed(self.random_state)
            random.seed(self.random_state)

        X_h = tf.placeholder(tf.int32, shape=[None, 1, self.h_max_length], name="X_heads")
        X_s = tf.placeholder(tf.int32, shape=[None, None, self.s_max_length], name="X_sents")
        X_h_length = tf.placeholder(tf.int32, shape=[None, 1], name="X_h_lengths")
        X_s_length = tf.placeholder(tf.int32, shape=[None, None], name="X_s_lengths")
        X_s_num_sents = tf.placeholder(tf.int32, shape=[None], name="X_s_num_sents")

        if self.dropout_rate:
            self._training = tf.placeholder_with_default(False, shape=[], name="training")
            self.keep_prob = tf.cond(self._training, lambda: tf.constant(1 - self.dropout_rate),
                                     lambda: tf.constant(1.0))
        else:
            self._training = None

        embedding = tf.get_variable(initializer=self.embedding, dtype=tf.float32, trainable=self.trainable,
                                    name="embedding")
        batch_size, num_sents = tf.unstack(tf.shape(X_s_length))
        flat_h = tf.reshape(X_h, [batch_size, self.h_max_length])
        flat_s = tf.reshape(X_s, [batch_size * num_sents, self.s_max_length])
        embed_h = tf.nn.embedding_lookup(embedding, ids=flat_h)
        embed_s = tf.nn.embedding_lookup(embedding, ids=flat_s)
        flat_h_length = tf.squeeze(X_h_length, axis=1)
        flat_s_length = tf.reshape(X_s_length, [batch_size * num_sents])

        if self.shared_rnn:
            with tf.variable_scope("encode_rnn", reuse=tf.AUTO_REUSE):
                h_encodings, _, _ = self._bidirectional_rnn(embed_h, flat_h_length, self.num_units)
                s_encodings, _, _ = self._bidirectional_rnn(embed_s, flat_s_length, self.num_units)
        else:
            h_encodings, _, _ = self._bidirectional_rnn(embed_h, flat_h_length, self.num_units, scope="h_encode_rnn")
            s_encodings, _, _ = self._bidirectional_rnn(embed_s, flat_s_length, self.num_units, scope="s_endode_rnn")

        h_encodings = tf.tile(h_encodings, [num_sents, 1, 1])
        flat_h_length = tf.tile(flat_h_length, [num_sents])
        sent_attends, claim_attends = self._inter_atten(h_encodings, s_encodings, flat_h_length, flat_s_length)

        claim_diff = tf.subtract(h_encodings, claim_attends)
        claim_mul = tf.multiply(h_encodings, claim_attends)

        sent_diff = tf.subtract(s_encodings, sent_attends)
        sent_mul = tf.multiply(s_encodings, sent_attends)

        m_claim = tf.concat([h_encodings, claim_attends, claim_diff, claim_mul], axis=2)
        m_sent = tf.concat([s_encodings, sent_attends, sent_diff, sent_mul], axis=2)

        if self.shared_rnn:
            with tf.variable_scope("infer_rnn", reuse=tf.AUTO_REUSE):
                h_infer, _, _ = self._bidirectional_rnn(m_claim, flat_h_length, self.num_units)
                s_infer, _, _ = self._bidirectional_rnn(m_sent, flat_s_length, self.num_units)
        else:
            h_infer, _, _ = self._bidirectional_rnn(m_claim, flat_h_length, self.num_units, scope="h_infer_rnn")
            s_infer, _, _ = self._bidirectional_rnn(m_sent, flat_s_length, self.num_units, scope="s_infer_rnn")

        claim_sum = tf.reduce_sum(h_infer, axis=1)
        claim_mask = tf.cast(tf.sequence_mask(flat_h_length), tf.float32)
        claim_ave = tf.div(claim_sum, tf.maximum(tf.reduce_sum(claim_mask, axis=1, keepdims=True), epsilon))
        claim_max = tf.reduce_max(h_infer, axis=1)

        sent_sum = tf.reduce_sum(s_infer, axis=1)
        sent_mask = tf.cast(tf.sequence_mask(flat_s_length), tf.float32)
        sent_ave = tf.div(sent_sum, tf.maximum(tf.reduce_sum(sent_mask, axis=1, keepdims=True), epsilon))
        sent_max = tf.reduce_max(s_infer, axis=1)

        v = tf.concat([claim_ave, claim_max, sent_ave, sent_max], axis=1)

        dense_output = self.mlp(v)
        flat_scores = tf.layers.dense(dense_output, 1)
        scores = tf.reshape(flat_scores, [batch_size, num_sents])
        infinite_mask = tf.zeros(dtype=tf.float32, shape=[batch_size, num_sents]) - np.inf
        zero_mask = tf.zeros_like(infinite_mask, dtype=tf.float32)
        num_sents_condition = tf.sequence_mask(X_s_num_sents, num_sents, dtype=tf.bool)
        masked_scores = tf.where(num_sents_condition, scores, infinite_mask)
        masked_scores = tf.nn.softmax(masked_scores)
        threshold_matrix = tf.fill([batch_size, num_sents], self.sent_threshold)
        scores_mask_condition = tf.less(threshold_matrix, masked_scores)
        masked_scores = tf.where(scores_mask_condition, masked_scores, zero_mask)
        self._X_head, self._X_body, self._X_h_sent_sizes, self._X_b_sent_sizes = X_h, X_s, X_h_length, X_s_length
        self._X_b_sizes = X_s_num_sents
        embed_s = tf.reshape(embed_s, [batch_size, num_sents, self.s_max_length, self.embedding_size])
        return masked_scores, embed_s, X_s_length, embed_h, X_h_length, X_s_num_sents

    def get_batch(self, h_np, b_np, h_sizes, b_sizes, h_sent_sizes, b_sent_sizes, y=None):

        num_batches = ceil(h_np.shape[0] / self.batch_size)

        for i in range(num_batches):
            if (i + 1) * self.batch_size > h_np.shape[0]:
                batch_h_np = h_np[i * self.batch_size:h_np.shape[0], :]
                batch_b_np = b_np[i * self.batch_size:h_np.shape[0], :]
                batch_h_sizes = h_sizes[i * self.batch_size:h_np.shape[0]]
                batch_b_sizes = b_sizes[i * self.batch_size:h_np.shape[0]]
                batch_h_sent_sizes = h_sent_sizes[i * self.batch_size:h_np.shape[0], :]
                batch_b_sent_sizes = b_sent_sizes[i * self.batch_size:h_np.shape[0], :]
                if y is not None:
                    batch_y = y[i * self.batch_size:h_np.shape[0]]
            else:
                batch_h_np = h_np[i * self.batch_size:(i + 1) * self.batch_size, :]
                batch_b_np = b_np[i * self.batch_size:(i + 1) * self.batch_size, :]
                batch_h_sizes = h_sizes[i * self.batch_size:(i + 1) * self.batch_size]
                batch_b_sizes = b_sizes[i * self.batch_size:(i + 1) * self.batch_size]
                batch_h_sent_sizes = h_sent_sizes[i * self.batch_size:(i + 1) * self.batch_size, :]
                batch_b_sent_sizes = b_sent_sizes[i * self.batch_size:(i + 1) * self.batch_size, :]
                if y is not None:
                    batch_y = y[i * self.batch_size:(i + 1) * self.batch_size]
            if y is not None:
                yield (batch_h_np, batch_b_np, batch_h_sizes, batch_b_sizes, batch_h_sent_sizes,
                       batch_b_sent_sizes, batch_y)
            else:
                yield (batch_h_np, batch_b_np, batch_h_sizes, batch_b_sizes, batch_h_sent_sizes,
                       batch_b_sent_sizes)

    def fit(self, X_dict, y):
        self.logger.debug("training...")
        X = X_dict['X_train']
        valid_X = X_dict['X_valid']
        y_valid = X_dict['y_valid']
        self.embedding = X_dict['embedding']
        self.close_session()
        y = np.array(y)

        if len(y.shape) == 2:
            y = np.argmax(y, axis=1)

        self._classes, _ = np.unique(y, return_counts=True)

        self._graph = tf.Graph()
        h_np, b_np, h_sizes, b_sizes, h_sent_sizes, b_sent_sizes = X['h_np'], X['b_np'], X['h_sizes'], X['b_sizes'], X[
            'h_sent_sizes'], X['b_sent_sizes']

        with self._graph.as_default():
            self._construct_graph()

        checks_without_progress = 0
        best_acc = 0
        best_parameters = None
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=self.max_gpu_memory)
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        self._session = tf.Session(graph=self._graph,
                                   config=tf.ConfigProto(gpu_options=gpu_options)
                                   # config=config
                                   )

        with self._session.as_default() as sess:
            self._init.run()
            num_instances = h_np.shape[0]
            for epoch in range(self.num_epoch):
                losses = []
                accs = []
                scores = []
                rnd_idx = np.random.permutation(num_instances)
                for rnd_indices in tqdm(np.array_split(rnd_idx, ceil(num_instances / self.batch_size))):

                    X_head_batch, X_body_batch, X_h_sizes_batch, X_b_sizes_batch, X_h_sent_sizes_batch, X_b_sent_sizes_batch, y_batch = \
                        h_np[rnd_indices], b_np[rnd_indices], \
                        h_sizes[rnd_indices], b_sizes[rnd_indices], \
                        h_sent_sizes[rnd_indices], b_sent_sizes[rnd_indices], \
                        y[rnd_indices]
                    y_batch = np.asarray(y_batch)
                    feed_dict = {self._X_head: X_head_batch, self._X_body: X_body_batch,
                                 self._X_b_sizes: X_b_sizes_batch, self._X_h_sent_sizes: X_h_sent_sizes_batch,
                                 self._X_b_sent_sizes: X_b_sent_sizes_batch, self.y: y_batch}
                    if self._training is not None:
                        feed_dict[self._training] = True

                    train_acc, _, loss, scores_batch = sess.run(
                        [self._accuracy, self._training_op, self._loss, self._X_scores], feed_dict=feed_dict)
                    losses.append(loss)
                    accs.append(train_acc)
                    scores.extend(scores_batch)

                average_loss = sum(losses) / len(losses)
                average_acc = sum(accs) / len(accs)

                if valid_X is not None and y_valid is not None:

                    self.logger.debug("validation phase")
                    valid_h_np, valid_b_np, valid_h_sizes, valid_b_sizes, valid_h_sent_sizes, valid_b_sent_sizes = \
                        valid_X['h_np'], valid_X['b_np'], valid_X['h_sizes'], valid_X['b_sizes'], valid_X[
                            'h_sent_sizes'], valid_X['b_sent_sizes']

                    batch_losses = []
                    batch_accuracies = []
                    valid_cm = np.zeros(shape=(self.n_outputs, self.n_outputs), dtype=np.int32)
                    for (
                            valid_h_batch, valid_b_batch, valid_h_sizes_batch, valid_b_sizes_batch,
                            valid_h_sent_sizes_batch, valid_b_sent_sizes_batch,
                            valid_y_batch) in tqdm(self.get_batch(valid_h_np, valid_b_np, valid_h_sizes,
                                                                  valid_b_sizes, valid_h_sent_sizes,
                                                                  valid_b_sent_sizes, y_valid)):

                        feed_dict_valid = {self._X_head: valid_h_batch, self._X_body: valid_b_batch,
                                           self._X_b_sizes: valid_b_sizes_batch,
                                           self._X_h_sent_sizes: valid_h_sent_sizes_batch,
                                           self._X_b_sent_sizes: valid_b_sent_sizes_batch, self.y: valid_y_batch}
                        if self.tensorboard_logdir:
                            val_acc_batch, val_loss_batch, cm, summary = sess.run(
                                [self._accuracy, self._loss, self._confusion_matrix, self._merged_summary],
                                feed_dict=feed_dict_valid)
                            self._file_writer.add_summary(summary, epoch)
                        else:
                            val_acc_batch, val_loss_batch, cm = sess.run(
                                [self._accuracy, self._loss, self._confusion_matrix], feed_dict=feed_dict_valid)

                        batch_losses.append(val_loss_batch)
                        batch_accuracies.append(val_acc_batch)
                        valid_cm = np.add(valid_cm, cm)

                    self.logger.info("\n" + str(valid_cm))
                    val_loss = sum(batch_losses) / len(batch_losses)
                    val_acc = sum(batch_accuracies) / len(batch_accuracies)

                    if self.show_progress:
                        if epoch % self.show_progress == 0:
                            self.logger.info(
                                "Epoch: {} Current training accuracy: {:.4f} ,Current training loss: {:.6f} Validation Accuracy: {:.4f} Validation Loss{:.6f}".format(
                                    epoch + 1, average_acc, average_loss, val_acc, val_loss))

                    if val_acc > best_acc:
                        best_acc = val_acc
                        checks_without_progress = 0
                        self.logger.info("accuracy has been improved!")
                        best_parameters = self._get_model_parameters()
                        self.save(self.ckpt_path)
                    else:
                        checks_without_progress += 1
                    if checks_without_progress > self.max_check_without_progress:
                        self.logger.info("Stopping Early! Loss has not improved in {} epoches".format(
                            self.max_check_without_progress))
                        break
                else:
                    if self.show_progress:
                        if epoch % self.show_progress == 0:
                            self.logger.info("Epoch: {} Current training accuracy: {:.4f}".format(
                                epoch + 1, train_acc))

            if best_parameters:
                self._restore_model_parameters(best_parameters)
            self.save(self.ckpt_path)
            return self

    def predict_proba(self, X_dict, restore_param_required=True):
        import pickle
        self.logger.debug("testing...")
        X = X_dict['X_test']
        self.embedding = X_dict['embedding']
        if restore_param_required:
            self.restore_model(self.ckpt_path)

        h_np, b_np, h_sizes, b_sizes, h_sent_sizes, b_sent_sizes = X['h_np'], X['b_np'], X['h_sizes'], X['b_sizes'], X[
            'h_sent_sizes'], X['b_sent_sizes']
        probabilities = []
        with self._session.as_default():
            for (pred_h_batch, pred_b_batch, pred_h_sizes_batch, pred_b_sizes_batch, pred_h_sent_sizes_batch,
                 pred_b_sent_sizes_batch) in tqdm(self.get_batch(h_np, b_np, h_sizes, b_sizes, h_sent_sizes,
                                                                 b_sent_sizes)):
                predictions_batch = self._probabilities.eval(
                    feed_dict={self._X_head: pred_h_batch, self._X_body: pred_b_batch,
                               self._X_b_sizes: pred_b_sizes_batch, self._X_h_sent_sizes: pred_h_sent_sizes_batch,
                               self._X_b_sent_sizes: pred_b_sent_sizes_batch})
                for prediction in predictions_batch:
                    probabilities.append(prediction)
        np_probas = np.asarray(probabilities)
        # save predictions
        _prediction_save_file = self.ckpt_path + "_predictions.p"
        with open(_prediction_save_file, 'wb') as f:
            pickle.dump(np_probas, f, protocol=pickle.HIGHEST_PROTOCOL)
            self.logger.info("predictions saved in " + _prediction_save_file)
        return np_probas

    def predict(self, X_dict, restore_param_required=True):

        predictions = np.argmax(self.predict_proba(X_dict, restore_param_required), axis=1)
        return np.reshape(predictions, (-1,))

    def _get_model_parameters(self):

        with self._graph.as_default():
            gvars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
        return {gvar.op.name: value for gvar, value in zip(gvars, self._session.run(gvars))}

    def _restore_model_parameters(self, model_parameters):

        gvar_names = list(model_parameters.keys())

        assign_ops = {gvar_name: self._graph.get_operation_by_name(gvar_name + "/Assign") for gvar_name in gvar_names}

        init_values = {gvar_name: assign_op.inputs[1] for gvar_name, assign_op in assign_ops.items()}
        feed_dict = {init_values[gvar_name]: model_parameters[gvar_name] for gvar_name in gvar_names}
        self._session.run(assign_ops, feed_dict=feed_dict)

    def close_session(self):
        if self._session:
            self._session.close()

    def save(self, path):
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._saver.save(self._session, path)

    def restore_model(self, path):
        self._graph = tf.Graph()
        with self._graph.as_default():
            self._construct_graph()
            gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=self.max_gpu_memory)
            config = tf.ConfigProto()
            config.gpu_options.allow_growth = True
            self._session = tf.Session(graph=self._graph,
                                       config=tf.ConfigProto(gpu_options=gpu_options)
                                       # config=config
                                       )
            with self._session.as_default() as sess:
                self._init.run()
                sess.run(tf.tables_initializer())
                self._saver.restore(sess, path)
        return self

    def _masked_reduce_max(self, data, partitions, border=None):
        _, masked_data = tf.dynamic_partition(data, partitions, 2)
        reduced_max = tf.reduce_max(masked_data, axis=0)
        if border is not None:
            border_matrix = tf.cast(tf.zeros_like(reduced_max) + border, tf.float32)
            return tf.maximum(reduced_max, border_matrix)
        return reduced_max
