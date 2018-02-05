from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.rnn as rnn_cell

from tensorflow.python.ops import rnn
from tensorflow.python.ops import variable_scope
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn
from tensorflow.python.ops import variable_scope
from tensorflow.contrib.rnn.python.ops.core_rnn_cell import _Linear
from tensorflow.python.ops.rnn_cell_impl import _linear as linear
from decoder import Decoder


class AttnDecoder(Decoder):
    """Implements the attention decoder of encoder-decoder framework."""

    @classmethod
    def class_params(cls):
        """Defines params of the class."""
        params = super(Decoder, cls).class_params()
        params['attention_vec_size'] = 128
        return params

    def __init__(self, params=None):
        """Initializer."""
        super(AttnDecoder, self).__init__(params)
        # No output projection required in attention decoder
        self.params.cell = self.set_cell_config(use_proj=False)

    def __call__(self, decoder_inp, seq_len,
                 encoder_hidden_states, seq_len_inp):
        # First prepare the decoder input - Embed the input and obtain the
        # relevant loop function
        params = self.params
        decoder_inputs, loop_function = self.prepare_decoder_input(decoder_inp)

        # TensorArray is used to do dynamic looping over decoder input
        inputs_ta = tf.TensorArray(size=params.max_output,
                                   dtype=tf.float32)
        inputs_ta = inputs_ta.unstack(decoder_inputs)

        batch_size = tf.shape(decoder_inputs)[1]
        emb_size = decoder_inputs.get_shape()[2].value

        # Attention variables
        attn_mask = tf.sequence_mask(tf.cast(seq_len_inp, tf.int32), dtype=tf.float32)

        with variable_scope.variable_scope("rnn_decoder"):
            # Calculate the W*h_enc component
            hidden = tf.expand_dims(attention_states, 2)
            W_attn = variable_scope.get_variable(
                "AttnW", [1, 1, attn_size, params.attention_vec_size])
            hidden_features = nn_ops.conv2d(hidden, W_attn, [1, 1, 1, 1], "SAME")
            v = variable_scope.get_variable("AttnV", [params.attention_vec_size])

            def raw_loop_function(time, cell_output, state, loop_state):
                def attention(query, prev_alpha):
                    """Put attention masks on hidden using hidden_features and query."""
                    with variable_scope.variable_scope("Attention"):
                        attn_proj = _Linear(query, params.attention_vec_size, True)
                        y = attn_proj(query)
                        y = array_ops.reshape(y, [-1, 1, 1, params.attention_vec_size])
                        s = math_ops.reduce_sum(
                            v * math_ops.tanh(hidden_features + y), [2, 3])

                        alpha = nn_ops.softmax(s) * attn_mask
                        sum_vec = tf.reduce_sum(alpha, reduction_indices=[1], keep_dims=True)
                        norm_term = tf.tile(sum_vec, tf.stack([1, tf.shape(alpha)[1]]))
                        alpha = alpha / norm_term

                        alpha = tf.expand_dims(alpha, 2)
                        alpha = tf.expand_dims(alpha, 3)
                        context_vec = math_ops.reduce_sum(alpha * hidden, [1, 2])
                    return tuple([context_vec, alpha])

                # If loop_function is set, we use it instead of decoder_inputs.
                elements_finished = (time >= seq_len)
                finished = tf.reduce_all(elements_finished)


                if cell_output is None:
                    next_state = cell.zero_state(batch_size, dtype=tf.float32)  #initial_state
                    output = None
                    loop_state = tuple([attn, alpha])
                    next_input = inputs_ta.read(time)
                else:
                    next_state = state
                    loop_state = attention(cell_output, loop_state[1])
                    with variable_scope.variable_scope("AttnOutputProjection"):
                        output = linear([cell_output] + list(loop_state[0]),
                                        output_size, True)

                    if not isTraining:
                        simple_input = loop_function(output, time)
                    else:
                        if loop_function is not None:
                            print("Scheduled Sampling will be done")
                            random_prob = tf.random_uniform([])
                            simple_input = tf.cond(finished,
                                lambda: tf.zeros([batch_size, emb_size], dtype=tf.float32),
                                lambda: tf.cond(tf.less(random_prob, 0.9),
                                    lambda: inputs_ta.read(time),
                                    lambda: loop_function(output))
                                )
                        else:
                            simple_input = tf.cond(finished,
                                lambda: tf.zeros([batch_size, emb_size], dtype=tf.float32),
                                lambda: inputs_ta.read(time)
                                )

                    # Merge input and previous attentions into one vector of the right size.
                    input_size = simple_input.get_shape().with_rank(2)[1]
                    if input_size.value is None:
                        raise ValueError("Could not infer input size from input")
                    with variable_scope.variable_scope("InputProjection"):
                        next_input_proj = _Linear([simple_input] + list(loop_state[0]), input_size, True)
                        next_input = next_input_proj([simple_input] + list(loop_state[0]))

                return (elements_finished, next_input, next_state, output, loop_state)

        # outputs is a TensorArray with T=max(sequence_length) entries
        # of shape Bx|V|
        outputs, state, _ = rnn.raw_rnn(self.cell, raw_loop_function)
        # Concatenate the output across timesteps to get a tensor of TxBx|v|
        # shape
        outputs = outputs.concat()
        return outputs