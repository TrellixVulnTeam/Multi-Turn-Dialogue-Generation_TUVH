import random
import torch
from torch import nn
from torch.nn import functional as F

from mtdg.models.rnn_factory import StackedLSTMCell, StackedGRUCell
from mtdg.models.beam_search import Beam

from mtdg.data import BOS_TOKEN, EOS_TOKEN, UNK_TOKEN
import math


class BaseRNNDecoder(nn.Module):
    def __init__(self, vocab):
        """Base Decoder Class"""
        super(BaseRNNDecoder, self).__init__()
        self.SOS_ID = vocab.stoi[BOS_TOKEN]
        self.UNK_ID = vocab.stoi[UNK_TOKEN]
        self.EOS_ID = vocab.stoi[EOS_TOKEN]


    @property
    def use_lstm(self):
        return isinstance(self.rnncell, StackedLSTMCell)

    def init_token(self, batch_size, inputs):
        """Get Variable of <SOS> Index (batch_size)"""
        x = torch.tensor([self.SOS_ID] * batch_size).type_as(inputs)
        return x

    def init_h(self, batch_size=None, zero=True, hidden=None):
        """Return RNN initial state"""
        if hidden is not None:
            return hidden

        if self.use_lstm:
            # (h, c)
            return (torch.zeros(self.num_layers,
                                       batch_size,
                                       self.hidden_size),
                    torch.zeros(self.num_layers,
                                       batch_size,
                                       self.hidden_size))
        else:
            # h
            return torch.zeros(self.num_layers,
                                      batch_size,
                                      self.hidden_size)

    def batch_size(self, inputs=None, h=None):
        """
        inputs: [batch_size, seq_len]
        h: [num_layers, batch_size, hidden_size] (RNN/GRU)
        h_c: [2, num_layers, batch_size, hidden_size] (LSTMCell)
        """
        if inputs is not None:
            batch_size = inputs.size(0)
            return batch_size

        else:
            if self.use_lstm:
                batch_size = h[0].size(1)
            else:
                batch_size = h.size(1)
            return batch_size

    def decode(self, out):
        """
        Args:
            out: unnormalized word distribution [batch_size, vocab_size]
        Return:
            x: word_index [batch_size]
        """

        # Sample next word from multinomial word distribution
        if self.sample:
            # x: [batch_size] - word index (next input)
            x = torch.multinomial(self.softmax(out / self.temperature, dim=1), 1).view(-1)

        # Greedy sampling
        else:
            # x: [batch_size] - word index (next input)
            _, x = out.max(dim=1)
        return x

    def forward(self):
        """Base forward function to inherit"""
        raise NotImplementedError

    def forward_step(self):
        """Run RNN single step"""
        raise NotImplementedError

    def embed(self, x):
        """word index: [batch_size] => word vectors: [batch_size, hidden_size]"""

        if self.training and self.word_drop > 0.0:
            if random.random() < self.word_drop:
                embed = self.embedding(x.data.new([self.UNK_ID] * x.size(0)))
            else:
                embed = self.embedding(x)
        else:
            embed = self.embedding(x)

        return embed

    def beam_decode(self,
                    init_h=None,
                    encoder_outputs=None, input_valid_length=None,
                    decode=False):
        """
        Args:
            encoder_outputs (Variable, FloatTensor): [batch_size, source_length, hidden_size]
            input_valid_length (Variable, LongTensor): [batch_size] (optional)
            init_h (variable, FloatTensor): [batch_size, hidden_size] (optional)
        Return:
            out   : [batch_size, seq_len]
        """
        batch_size = self.batch_size(h=init_h)

        # [batch_size x beam_size]
        x = self.init_token(batch_size * self.beam_size, init_h)

        # [num_layers, batch_size x beam_size, hidden_size]
        h = self.init_h(batch_size, hidden=init_h).repeat(1, self.beam_size, 1)

        # batch_position [batch_size]
        #   [0, beam_size, beam_size * 2, .., beam_size * (batch_size-1)]
        #   Points where batch starts in [batch_size x beam_size] tensors
        #   Ex. position_idx[5]: when 5-th batch starts
        batch_position = torch.arange(0, batch_size).long() * self.beam_size

        # Initialize scores of sequence
        # [batch_size x beam_size]
        # Ex. batch_size: 5, beam_size: 3
        # [0, -inf, -inf, 0, -inf, -inf, 0, -inf, -inf, 0, -inf, -inf, 0, -inf, -inf]
        score = torch.ones(batch_size * self.beam_size) * -float('inf')
        score.index_fill_(0, torch.arange(0, batch_size).long() * self.beam_size, 0.0)
        score = score

        # Initialize Beam that stores decisions for backtracking
        beam = Beam(
            batch_size,
            self.hidden_size,
            self.vocab_size,
            self.beam_size,
            self.max_unroll,
            batch_position,
            self.EOS_ID)

        for i in range(self.max_unroll):

            # x: [batch_size x beam_size]; (token index)
            # =>
            # out: [batch_size x beam_size, vocab_size]
            # h: [num_layers, batch_size x beam_size, hidden_size]
            out, h = self.forward_step(x, h,
                                       encoder_outputs=encoder_outputs,
                                       input_valid_length=input_valid_length)
            # log_prob: [batch_size x beam_size, vocab_size]
            log_prob = F.log_softmax(out, dim=1)

            # [batch_size x beam_size]
            # => [batch_size x beam_size, vocab_size]
            score = score.view(-1, 1) + log_prob

            # Select `beam size` transitions out of `vocab size` combinations

            # [batch_size x beam_size, vocab_size]
            # => [batch_size, beam_size x vocab_size]
            # Cutoff and retain candidates with top-k scores
            # score: [batch_size, beam_size]
            # top_k_idx: [batch_size, beam_size]
            #       each element of top_k_idx [0 ~ beam x vocab)

            score, top_k_idx = score.view(batch_size, -1).topk(self.beam_size, dim=1)

            # Get token ids with remainder after dividing by top_k_idx
            # Each element is among [0, vocab_size)
            # Ex. Index of token 3 in beam 4
            # (4 * vocab size) + 3 => 3
            # x: [batch_size x beam_size]
            x = (top_k_idx % self.vocab_size).view(-1)

            # top-k-pointer [batch_size x beam_size]
            #       Points top-k beam that scored best at current step
            #       Later used as back-pointer at backtracking
            #       Each element is beam index: 0 ~ beam_size
            #                     + position index: 0 ~ beam_size x (batch_size-1)
            beam_idx = top_k_idx / self.vocab_size  # [batch_size, beam_size]
            top_k_pointer = (beam_idx + batch_position.unsqueeze(1)).view(-1)

            # Select next h (size doesn't change)
            # [num_layers, batch_size * beam_size, hidden_size]
            h = h.index_select(1, top_k_pointer)

            # Update sequence scores at beam
            beam.update(score.clone(), top_k_pointer, x)  # , h)

            # Erase scores for EOS so that they are not expanded
            # [batch_size, beam_size]
            eos_idx = x.data.eq(self.EOS_ID).view(batch_size, self.beam_size)
            if eos_idx.nonzero().dim() > 0:
                score.data.masked_fill_(eos_idx, -float('inf'))

        # prediction ([batch, k, max_unroll])
        #     A list of Tensors containing predicted sequence
        # final_score [batch, k]
        #     A list containing the final scores for all top-k sequences
        # length [batch, k]
        #     A list specifying the length of each sequence in the top-k candidates
        # prediction, final_score, length = beam.backtrack()
        prediction, final_score, length = beam.backtrack()

        return prediction, final_score, length


class DecoderRNN(BaseRNNDecoder):
    def __init__(self, vocab, vocab_size, embedding_size,
                 hidden_size, rnncell=StackedGRUCell, num_layers=1,
                 dropout=0.0, word_drop=0.0,
                 max_unroll=30, sample=True, temperature=1.0, beam_size=1):
        super(DecoderRNN, self).__init__(vocab)

        self.vocab_size = vocab_size
        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.temperature = temperature
        self.word_drop = word_drop
        self.max_unroll = max_unroll
        self.sample = sample
        self.beam_size = beam_size

        self.embedding = nn.Embedding(vocab_size, embedding_size)

        self.rnncell = rnncell(num_layers,
                               embedding_size,
                               hidden_size,
                               dropout)
        self.out = nn.Linear(hidden_size, vocab_size)
        self.softmax = nn.Softmax()

    def forward_step(self, x, h,
                     encoder_outputs=None,
                     input_valid_length=None):
        """
        Single RNN Step
        1. Input Embedding (vocab_size => hidden_size)
        2. RNN Step (hidden_size => hidden_size)
        3. Output Projection (hidden_size => vocab size)

        Args:
            x: [batch_size]
            h: [num_layers, batch_size, hidden_size] (h and c from all layers)

        Return:
            out: [batch_size,vocab_size] (Unnormalized word distribution)
            h: [num_layers, batch_size, hidden_size] (h and c from all layers)
        """
        # x: [batch_size] => [batch_size, hidden_size]
        x = self.embed(x)

        # last_h: [batch_size, hidden_size] (h from Top RNN layer)
        # h: [num_layers, batch_size, hidden_size] (h and c from all layers)
        last_h, h = self.rnncell(x, h)

        if self.use_lstm:
            # last_h_c: [2, batch_size, hidden_size] (h from Top RNN layer)
            # h_c: [2, num_layers, batch_size, hidden_size] (h and c from all layers)
            last_h = last_h[0]

        # Unormalized word distribution
        # out: [batch_size, vocab_size]
        out = self.out(last_h)
        return out, h

    def forward(self, inputs, init_h=None, encoder_outputs=None, input_valid_length=None,
                decode=False):
        """
        Train (decode=False)
            Args:
                inputs (Variable, LongTensor): [batch_size, seq_len]
                init_h: (Variable, FloatTensor): [num_layers, batch_size, hidden_size]
            Return:
                out   : [batch_size, seq_len, vocab_size]
        Test (decode=True)
            Args:
                inputs: None
                init_h: (Variable, FloatTensor): [num_layers, batch_size, hidden_size]
            Return:
                out   : [batch_size, seq_len]
        """
        batch_size = self.batch_size(inputs, init_h)

        # x: [batch_size]
        x = self.init_token(batch_size, inputs)

        # h: [num_layers, batch_size, hidden_size]
        h = self.init_h(batch_size, hidden=init_h)


        if not decode:
            out_list = []
            seq_len = inputs.size(1)
            for i in range(seq_len):

                # x: [batch_size]
                # =>
                # out: [batch_size, vocab_size]
                # h: [num_layers, batch_size, hidden_size] (h and c from all layers)
                out, h = self.forward_step(x, h)

                out_list.append(out)
                x = inputs[:, i]

            # [batch_size, max_target_len, vocab_size]
            return torch.stack(out_list, dim=1)
        else:
            x_list = []
            for i in range(self.max_unroll):

                # x: [batch_size]
                # =>
                # out: [batch_size, vocab_size]
                # h: [num_layers, batch_size, hidden_size] (h and c from all layers)
                out, h = self.forward_step(x, h)

                # out: [batch_size, vocab_size]
                # => x: [batch_size]
                # x = self.decode(out)
                _, x = out.max(dim=1)

                x_list.append(x)

            # [batch_size, max_target_len]
            return torch.stack(x_list, dim=1)


class TopicGatedDecoder(BaseRNNDecoder):
    def __init__(self, vocab, vocab_size, embedding_size,
                 hidden_size, rnncell=StackedGRUCell, num_layers=1,
                 dropout=0.0, word_drop=0.0,
                 max_unroll=30, sample=True, temperature=1.0, beam_size=1,
                 topic_gate=None):
        super(TopicGatedDecoder, self).__init__(vocab)

        self.vocab_size = vocab_size
        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.temperature = temperature
        self.word_drop = word_drop
        self.max_unroll = max_unroll
        self.sample = sample
        self.beam_size = beam_size

        self.embedding = nn.Embedding(vocab_size, embedding_size)

        self.rnncell = rnncell(num_layers,
                               embedding_size,
                               hidden_size,
                               dropout)
        self.out = nn.Linear(hidden_size, vocab_size)
        self.softmax = nn.Softmax()

        self.topic_gate = topic_gate

    def forward_step(self, x, h,
                     encoder_outputs=None,
                     input_valid_length=None,
                     topic_rep=None):
        """
        Single RNN Step
        1. Input Embedding (vocab_size => hidden_size)
        2. RNN Step (hidden_size => hidden_size)
        3. Output Projection (hidden_size => vocab size)

        Args:
            x: [batch_size]
            h: [num_layers, batch_size, hidden_size] (h and c from all layers)

        Return:
            out: [batch_size,vocab_size] (Unnormalized word distribution)
            h: [num_layers, batch_size, hidden_size] (h and c from all layers)
        """
        # x: [batch_size] => [batch_size, hidden_size]
        x = self.embed(x)

        # last_h: [batch_size, hidden_size] (h from Top RNN layer)
        # h: [num_layers, batch_size, hidden_size] (h and c from all layers)
        last_h, h = self.rnncell(x, h)

        if self.use_lstm:
            # last_h_c: [2, batch_size, hidden_size] (h from Top RNN layer)
            # h_c: [2, num_layers, batch_size, hidden_size] (h and c from all layers)
            last_h = last_h[0]

        # Unormalized word distribution
        # out: [batch_size, vocab_size]
        if self.topic_gate is not None and topic_rep is not None:
            out = self.topic_gate(x, last_h, topic_rep)

        out = self.out(last_h)
        return out, h

    def forward(self, inputs, init_h=None, encoder_outputs=None, input_valid_length=None,
                decode=False, topic_rep=None):
        """
        Train (decode=False)
            Args:
                inputs (Variable, LongTensor): [batch_size, seq_len]
                init_h: (Variable, FloatTensor): [num_layers, batch_size, hidden_size]
            Return:
                out   : [batch_size, seq_len, vocab_size]
        Test (decode=True)
            Args:
                inputs: None
                init_h: (Variable, FloatTensor): [num_layers, batch_size, hidden_size]
            Return:
                out   : [batch_size, seq_len]
        """
        batch_size = self.batch_size(inputs, init_h)

        # x: [batch_size]
        x = self.init_token(batch_size, inputs)

        # h: [num_layers, batch_size, hidden_size]
        h = self.init_h(batch_size, hidden=init_h)


        if not decode:
            out_list = []
            seq_len = inputs.size(1)
            for i in range(seq_len):

                # x: [batch_size]
                # =>
                # out: [batch_size, vocab_size]
                # h: [num_layers, batch_size, hidden_size] (h and c from all layers)
                out, h = self.forward_step(x, h, topic_rep=topic_rep)

                out_list.append(out)
                x = inputs[:, i]

            # [batch_size, max_target_len, vocab_size]
            return torch.stack(out_list, dim=1)
        else:
            x_list = []
            for i in range(self.max_unroll):

                # x: [batch_size]
                # =>
                # out: [batch_size, vocab_size]
                # h: [num_layers, batch_size, hidden_size] (h and c from all layers)
                out, h = self.forward_step(x, h, topic_rep=topic_rep)

                # out: [batch_size, vocab_size]
                # => x: [batch_size]
                # x = self.decode(out)
                _, x = out.max(dim=1)

                x_list.append(x)

            # [batch_size, max_target_len]
            return torch.stack(x_list, dim=1)