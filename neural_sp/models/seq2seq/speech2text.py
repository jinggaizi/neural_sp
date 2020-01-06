#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2018 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Speech to text sequence-to-sequence model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import logging
import numpy as np
import torch
import torch.nn as nn

from neural_sp.bin.train_utils import load_checkpoint
from neural_sp.models.base import ModelBase
from neural_sp.models.lm.rnnlm import RNNLM
from neural_sp.models.seq2seq.decoders.build import build_decoder
from neural_sp.models.seq2seq.decoders.fwd_bwd_attention import fwd_bwd_attention
from neural_sp.models.seq2seq.decoders.rnn_transducer import RNNTransducer
from neural_sp.models.seq2seq.decoders.transformer_transducer import TrasformerTransducer
from neural_sp.models.seq2seq.encoders.build import build_encoder
from neural_sp.models.seq2seq.frontends.frame_stacking import stack_frame
from neural_sp.models.seq2seq.frontends.gaussian_noise import add_gaussian_noise
from neural_sp.models.seq2seq.frontends.sequence_summary import SequenceSummaryNetwork
from neural_sp.models.seq2seq.frontends.spec_augment import SpecAugment
from neural_sp.models.seq2seq.frontends.splicing import splice
from neural_sp.models.torch_utils import np2tensor
from neural_sp.models.torch_utils import tensor2np
from neural_sp.models.torch_utils import pad_list


logger = logging.getLogger(__name__)


class Speech2Text(ModelBase):
    """Speech to text sequence-to-sequence model."""

    def __init__(self, args, save_path=None):

        super(ModelBase, self).__init__()

        self.save_path = save_path

        # for encoder, decoder
        self.input_type = args.input_type
        self.input_dim = args.input_dim
        self.enc_type = args.enc_type
        self.enc_n_units = args.enc_n_units
        if args.enc_type in ['blstm', 'bgru', 'conv_blstm', 'conv_bgru']:
            self.enc_n_units *= 2
        self.dec_type = args.dec_type

        # for OOV resolution
        self.enc_n_layers = args.enc_n_layers
        self.enc_n_layers_sub1 = args.enc_n_layers_sub1
        self.subsample = [int(s) for s in args.subsample.split('_')]

        # for decoder
        self.vocab = args.vocab
        self.vocab_sub1 = args.vocab_sub1
        self.vocab_sub2 = args.vocab_sub2
        self.blank = 0
        self.unk = 1
        self.eos = 2
        self.pad = 3
        # NOTE: reserved in advance

        # for the sub tasks
        self.main_weight = 1 - args.sub1_weight - args.sub2_weight
        self.sub1_weight = args.sub1_weight
        self.sub2_weight = args.sub2_weight
        self.mtl_per_batch = args.mtl_per_batch
        self.task_specific_layer = args.task_specific_layer

        # for CTC
        self.ctc_weight = min(args.ctc_weight, self.main_weight)
        self.ctc_weight_sub1 = min(args.ctc_weight_sub1, self.sub1_weight)
        self.ctc_weight_sub2 = min(args.ctc_weight_sub2, self.sub2_weight)

        # for backward decoder
        self.bwd_weight = min(args.bwd_weight, self.main_weight)
        self.fwd_weight = self.main_weight - self.bwd_weight - self.ctc_weight
        self.fwd_weight_sub1 = self.sub1_weight - self.ctc_weight_sub1
        self.fwd_weight_sub2 = self.sub2_weight - self.ctc_weight_sub2

        # Feature extraction
        self.gaussian_noise = args.gaussian_noise
        self.n_stacks = args.n_stacks
        self.n_skips = args.n_skips
        self.n_splices = args.n_splices
        self.use_specaug = args.n_freq_masks > 0 or args.n_time_masks > 0
        self.specaug = None
        if self.use_specaug:
            assert args.n_stacks == 1 and args.n_skips == 1
            assert args.n_splices == 1
            self.specaug = SpecAugment(F=args.freq_width,
                                       T=args.time_width,
                                       n_freq_masks=args.n_freq_masks,
                                       n_time_masks=args.n_time_masks,
                                       p=args.time_width_upper)

        # Frontend
        self.ssn = None
        if args.sequence_summary_network:
            assert args.input_type == 'speech'
            self.ssn = SequenceSummaryNetwork(args.input_dim,
                                              n_units=512,
                                              n_layers=3,
                                              bottleneck_dim=100,
                                              dropout=0,
                                              param_init=args.param_init)

        # Encoder
        self.enc = build_encoder(args)
        if args.freeze_encoder:
            for p in self.enc.parameters():
                p.requires_grad = False

        # main task
        directions = []
        if self.fwd_weight > 0 or self.ctc_weight > 0:
            directions.append('fwd')
        if self.bwd_weight > 0:
            directions.append('bwd')
        for dir in directions:
            # Load the LM for LM fusion
            if args.lm_fusion and dir == 'fwd':
                lm_fusion = RNNLM(args.lm_conf)
                lm_fusion = load_checkpoint(lm_fusion, args.lm_fusion)[0]
            else:
                lm_fusion = None
                # TODO(hirofumi): for backward RNNLM

            # Load the LM for LM initialization
            if args.lm_init and dir == 'fwd':
                lm_init = RNNLM(args.lm_conf)
                lm_init = load_checkpoint(lm_init, args.lm_init)[0]
            else:
                lm_init = None
                # TODO(hirofumi): for backward RNNLM

            # Decoder
            special_symbols = {
                'blank': self.blank,
                'unk': self.unk,
                'eos': self.eos,
                'pad': self.pad,
            }
            dec = build_decoder(args, special_symbols, self.enc.output_dim,
                                args.vocab,
                                self.ctc_weight if dir == 'fwd' else 0,
                                args.ctc_fc_list,
                                self.main_weight - self.bwd_weight if dir == 'fwd' else self.bwd_weight,
                                lm_init, lm_fusion)
            setattr(self, 'dec_' + dir, dec)

        # sub task
        for sub in ['sub1', 'sub2']:
            if getattr(self, sub + '_weight') > 0:
                dec_sub = build_decoder(args, special_symbols, self.enc_n_units,
                                        getattr(self, 'vocab_' + sub),
                                        getattr(self, 'ctc_weight_' + sub),
                                        getattr(args, 'ctc_fc_list_' + sub),
                                        getattr(self, sub + '_weight'),
                                        lm_init, lm_fusion)
                setattr(self, 'dec_fwd_' + sub, dec_sub)

        if args.input_type == 'text':
            if args.vocab == args.vocab_sub1:
                # Share the embedding layer between input and output
                self.embed = dec.embed
            else:
                self.embed = nn.Embedding(args.vocab_sub1, args.emb_dim,
                                          padding_idx=self.pad)
                self.dropout_emb = nn.Dropout(p=args.dropout_emb)

        # Recurrent weights are orthogonalized
        if args.rec_weight_orthogonal:
            self.reset_parameters(args.param_init, dist='orthogonal',
                                  keys=['rnn', 'weight'])

        # Initialize bias in forget gate with 1
        # self.init_forget_gate_bias_with_one()

        # Fix all parameters except for the gating parts in deep fusion
        if args.lm_fusion_type == 'deep' and args.lm_fusion:
            for n, p in self.named_parameters():
                if 'output' in n or 'output_bn' in n or 'linear' in n:
                    p.requires_grad = True
                else:
                    p.requires_grad = False

    def scheduled_sampling_trigger(self):
        # main task
        directions = []
        if self.fwd_weight > 0:
            directions.append('fwd')
        if self.bwd_weight > 0:
            directions.append('bwd')
        for dir in directions:
            getattr(self, 'dec_' + dir).start_scheduled_sampling()

        # sub task
        for sub in ['sub1', 'sub2']:
            if getattr(self, sub + '_weight') > 0:
                directions = []
                if getattr(self, 'fwd_weight_' + sub) > 0:
                    directions.append('fwd')
                for dir_sub in directions:
                    getattr(self, 'dec_' + dir_sub + '_' + sub).start_scheduled_sampling()

    def forward(self, batch, task='all', is_eval=False,
                teacher=None, teacher_lm=None):
        """Forward computation.

        Args:
            batch (dict):
                xs (list): input data of size `[T, input_dim]`
                xlens (list): lengths of each element in xs
                ys (list): reference labels in the main task of size `[L]`
                ys_sub1 (list): reference labels in the 1st auxiliary task of size `[L_sub1]`
                ys_sub2 (list): reference labels in the 2nd auxiliary task of size `[L_sub2]`
                utt_ids (list): name of utterances
                speakers (list): name of speakers
            task (str): all/ys*/ys_sub*
            is_eval (bool): evaluation mode
                This should be used in inference model for memory efficiency.
            teacher (Speech2Text): used for knowledge distillation from ASR
            teacher_lm (RNNLM): used for knowledge distillation from LM
        Returns:
            loss (FloatTensor): `[1]`
            observation (dict):

        """
        if is_eval:
            self.eval()
            with torch.no_grad():
                loss, observation = self._forward(batch, task)
        else:
            self.train()
            loss, observation = self._forward(batch, task, teacher, teacher_lm)

        return loss, observation

    def generate_logits(self, batch, temperature=1.0):
        # Encode input features
        if self.input_type == 'speech':
            eout_dict = self.encode(batch['xs'], task='ys')
        else:
            eout_dict = self.encode(batch['ys_sub1'], task='ys')

        # for the forward decoder in the main task
        logits = self.dec_fwd.forward_att(
            eout_dict['ys']['xs'], eout_dict['ys']['xlens'], batch['ys'],
            return_logits=True)
        return logits

    def generate_lm_logits(self, ys, lm, temperature=5.0):
        # Append <sos> and <eos>
        eos = next(lm.parameters()).new_zeros(1).fill_(self.eos).long()
        ys = [np2tensor(np.fromiter(y, dtype=np.int64), self.device_id)for y in ys]
        ys_in = pad_list([torch.cat([eos, y], dim=0) for y in ys], self.pad)
        lmout, _ = lm.decode(ys_in, None)
        logits = lm.output(lmout)
        return logits

    def _forward(self, batch, task, teacher=None, teacher_lm=None):
        # Encode input features
        if self.input_type == 'speech':
            if self.mtl_per_batch:
                flip = True if 'bwd' in task else False
                eout_dict = self.encode(batch['xs'], task, flip=flip)
            else:
                flip = True if self.bwd_weight == 1 else False
                eout_dict = self.encode(batch['xs'], task='all', flip=flip)
        else:
            eout_dict = self.encode(batch['ys_sub1'])

        observation = {}
        loss = torch.zeros((1,), dtype=torch.float32).cuda(self.device_id)

        # for the forward decoder in the main task
        if (self.fwd_weight > 0 or self.ctc_weight > 0) and task in ['all', 'ys', 'ys.ctc']:
            teacher_logits = None
            if teacher is not None:
                teacher.eval()
                teacher_logits = teacher.generate_logits(batch)
                # TODO(hirofumi): label smoothing, scheduled sampling, dropout?
            elif teacher_lm is not None:
                teacher_lm.eval()
                teacher_logits = self.generate_lm_logits(batch['ys'], lm=teacher_lm)

            loss_fwd, obs_fwd = self.dec_fwd(eout_dict['ys']['xs'], eout_dict['ys']['xlens'],
                                             batch['ys'], task, batch['ys_hist'], teacher_logits)
            loss += loss_fwd
            if isinstance(self.dec_fwd, RNNTransducer) or isinstance(self.dec_fwd, TrasformerTransducer):
                observation['loss.transducer'] = obs_fwd['loss_transducer']
            else:
                observation['loss.att'] = obs_fwd['loss_att']
                if 'loss_quantity' not in obs_fwd.keys():
                    obs_fwd['loss_quantity'] = None
                observation['loss.quantity'] = obs_fwd['loss_quantity']
                if 'loss_latency' not in obs_fwd.keys():
                    obs_fwd['loss_latency'] = None
                observation['loss.latency'] = obs_fwd['loss_latency']
                observation['acc.att'] = obs_fwd['acc_att']
                observation['ppl.att'] = obs_fwd['ppl_att']
            observation['loss.ctc'] = obs_fwd['loss_ctc']

        # for the backward decoder in the main task
        if self.bwd_weight > 0 and task in ['all', 'ys.bwd']:
            loss_bwd, obs_bwd = self.dec_bwd(eout_dict['ys']['xs'], eout_dict['ys']['xlens'], batch['ys'], task)
            loss += loss_bwd
            observation['loss.att-bwd'] = obs_bwd['loss_att']
            observation['acc.att-bwd'] = obs_bwd['acc_att']
            observation['ppl.att-bwd'] = obs_bwd['ppl_att']
            observation['loss.ctc-bwd'] = obs_bwd['loss_ctc']

        # only fwd for sub tasks
        for sub in ['sub1', 'sub2']:
            # for the forward decoder in the sub tasks
            if (getattr(self, 'fwd_weight_' + sub) > 0 or getattr(self, 'ctc_weight_' + sub) > 0) and task in ['all', 'ys_' + sub, 'ys_' + sub + '.ctc']:
                loss_sub, obs_fwd_sub = getattr(self, 'dec_fwd_' + sub)(
                    eout_dict['ys_' + sub]['xs'], eout_dict['ys_' + sub]['xlens'],
                    batch['ys_' + sub], task)
                loss += loss_sub
                if isinstance(getattr(self, 'dec_fwd_' + sub), RNNTransducer):
                    observation['loss.transducer-' + sub] = obs_fwd_sub['loss_transducer']
                else:
                    observation['loss.att-' + sub] = obs_fwd_sub['loss_att']
                    observation['acc.att-' + sub] = obs_fwd_sub['acc_att']
                    observation['ppl.att-' + sub] = obs_fwd_sub['ppl_att']
                observation['loss.ctc-' + sub] = obs_fwd_sub['loss_ctc']

        return loss, observation

    def encode(self, xs, task='all', flip=False, use_cache=False, streaming=False):
        """Encode acoustic or text features.

        Args:
            xs (list): A list of length `[B]`, which contains Tensor of size `[T, input_dim]`
            task (str): all/ys*/ys_sub1*/ys_sub2*
            flip (bool): if True, flip acoustic features in the time-dimension
            use_cache (bool): use the cached forward encoder state in the previous chunk as the initial state
            streaming (bool): streaming encoding
        Returns:
            eout_dict (dict):

        """
        if self.input_type == 'speech':
            # Frame stacking
            if self.n_stacks > 1:
                xs = [stack_frame(x, self.n_stacks, self.n_skips) for x in xs]

            # Splicing
            if self.n_splices > 1:
                xs = [splice(x, self.n_splices, self.n_stacks) for x in xs]
            xlens = torch.IntTensor([len(x) for x in xs])

            # Flip acoustic features in the reverse order
            if flip:
                xs = [torch.from_numpy(np.flip(x, axis=0).copy()).float().cuda(self.device_id) for x in xs]
            else:
                xs = [np2tensor(x, self.device_id).float() for x in xs]
            xs = pad_list(xs, 0.)

            # SpecAugment
            if self.use_specaug and self.training:
                xs = self.specaug(xs)

            # Gaussian noise injection
            if self.gaussian_noise:
                xs = add_gaussian_noise(xs)

            # Sequence summary network
            if self.ssn is not None:
                xs += self.ssn(xs, xlens)

        elif self.input_type == 'text':
            xlens = torch.IntTensor([len(x) for x in xs])
            xs = [np2tensor(np.fromiter(x, dtype=np.int64), self.device_id) for x in xs]
            xs = pad_list(xs, self.pad)
            xs = self.dropout_emb(self.embed(xs))
            # TODO(hirofumi): fix for Transformer

        # encoder
        eout_dict = self.enc(xs, xlens, task.split('.')[0], use_cache, streaming)

        if self.main_weight < 1 and self.enc_type in ['conv', 'tds', 'gated_conv', 'transformer', 'conv_transformer']:
            for sub in ['sub1', 'sub2']:
                eout_dict['ys_' + sub]['xs'] = eout_dict['ys']['xs'].clone()
                eout_dict['ys_' + sub]['xlens'] = eout_dict['ys']['xlens'][:]

        return eout_dict

    def get_ctc_probs(self, xs, task='ys', temperature=1, topk=None):
        self.eval()
        with torch.no_grad():
            eout_dict = self.encode(xs, task)
            dir = 'fwd' if self.fwd_weight >= self.bwd_weight else 'bwd'
            if task == 'ys_sub1':
                dir += '_sub1'
            elif task == 'ys_sub2':
                dir += '_sub2'

            if task == 'ys':
                assert self.ctc_weight > 0
            elif task == 'ys_sub1':
                assert self.ctc_weight_sub1 > 0
            elif task == 'ys_sub2':
                assert self.ctc_weight_sub2 > 0
            ctc_probs, indices_topk = getattr(self, 'dec_' + dir).ctc_probs_topk(
                eout_dict[task]['xs'], temperature, topk)
            return tensor2np(ctc_probs), tensor2np(indices_topk), eout_dict[task]['xlens']

    def plot_attention(self):
        if 'transformer' in self.enc_type:
            self.enc._plot_attention(self.save_path)
        if 'transformer' in self.dec_type or 'transducer' not in self.dec_type:
            self.dec_fwd._plot_attention(self.save_path)

    def decode_streaming(self, xs, params, idx2token, exclude_eos=False, task='ys'):
        self.eval()
        with torch.no_grad():
            assert task == 'ys'
            assert self.input_type == 'speech'
            assert self.ctc_weight > 0
            assert self.fwd_weight > 0
            assert len(xs) == 1  # batch size
            assert params['recog_length_norm']
            global_params = copy.deepcopy(params)
            global_params['recog_max_len_ratio'] = 1.0

            lm = getattr(self, 'lm_fwd', None)
            lm_2nd = getattr(self, 'lm_2nd', None)

            # hyper parameters
            ctc_vad = params['recog_ctc_vad']
            blank_threshold = params['recog_ctc_vad_blank_threshold']
            spike_threshold = params['recog_ctc_vad_spike_threshold']

            cs_l = self.enc.lc_chunk_size_left
            cs_r = self.enc.lc_chunk_size_right
            factor = self.enc.subsampling_factor()
            blank_threshold /= factor
            x_whole = xs[0]  # `[T, input_dim]`
            # self.enc.turn_off_ceil_mode(self.enc)

            eout_chunks = []
            ctc_probs_chunks = []
            t = 0  # global time offset
            n_blanks = 0  # inter-chunk
            boundary_offset = -1  # boudnary offset in each chunk (after subsampling)
            reset_beam = True
            best_hyp_id_stream = []
            while True:
                # Encode input features chunk by chunk
                x_chunk = x_whole[t:t + (cs_l + cs_r)]
                eout_dict_chunk = self.encode([x_chunk], task,
                                              use_cache=not reset_beam,
                                              streaming=True)
                eout_chunk = eout_dict_chunk[task]['xs']
                boundary_offset = -1  # reset

                # CTC-based VAD
                if ctc_vad:
                    ctc_probs_chunk = self.dec_fwd.ctc_probs(eout_chunk)
                    _, topk_ids_chunk = torch.topk(ctc_probs_chunk, k=1, dim=-1, largest=True, sorted=True)
                    ctc_probs_chunks.append(ctc_probs_chunk)
                    n_blanks_chunk = 0  # intra-chunk
                    for j in range(ctc_probs_chunk.size(1)):
                        if topk_ids_chunk[0, j, 0] == self.blank:
                            n_blanks += 1
                            n_blanks_chunk += 1
                        elif ctc_probs_chunk[0, j, topk_ids_chunk[0, j, 0]] < spike_threshold:
                            n_blanks += 1
                            n_blanks_chunk += 1
                        else:
                            n_blanks = 0
                            # print('CTC (T:%d): %s' % (t + j * factor, idx2token([topk_ids_chunk[0, j, 0].item()])))
                        if n_blanks > blank_threshold:
                            boundary_offset = j  # select the most right blank offset
                    ctc_log_probs_chunk = torch.log(ctc_probs_chunk)
                else:
                    ctc_log_probs_chunk = None

                # Truncate the most right frames
                if boundary_offset >= 0:
                    eout_chunk = eout_chunk[:, :boundary_offset + 1]
                eout_chunks.append(eout_chunk)

                # Chunk-synchronous attention decoding
                best_hyp_id_prefix, aws_prefix = self.dec_fwd.beam_search_chunk_sync(
                    eout_chunk, params, idx2token,
                    lm=lm, ctc_log_probs=ctc_log_probs_chunk,
                    reset_beam=reset_beam)
                reset_beam = boundary_offset >= 0
                # print('Sync MoChA (Glo-T:%d, Loc-T:%d, blank:%d frames): %s' %
                #       (t + eout_chunk.size(1) * factor,
                #        self.dec_fwd.n_frames * factor,
                #        n_blanks * factor, idx2token(best_hyp_id_prefix)))
                # print('-' * 50)

                if len(best_hyp_id_prefix) > 0 and best_hyp_id_prefix[-1] == self.eos:
                    if len(best_hyp_id_prefix) > 1:
                        best_hyp_id_prefix = best_hyp_id_prefix[:-1]
                    if not ctc_vad:
                        reset_beam = True

                # Segmentation strategy 1:
                # If any segmentation points are not found in the current chunk,
                # encoder states will be carried over to the next chunk.
                # Otherwise, the current chunk is segmented at the point where
                # n_blanks surpasses the threshold.

                # Segmentation strategy 2:
                # If <eos> is emitted from the decoder (not CTC),
                # the current chunk is segmented.

                if reset_beam:
                    # Global decoding over the segmented region
                    # eout = torch.cat(eout_chunks, dim=1)
                    # elens = torch.IntTensor([eout.size(1)])
                    # nbest_hyps_id_offline, _, _ = self.dec_fwd.beam_search(
                    #     eout, elens, global_params, idx2token, lm, lm_2nd)
                    # print('MoChA: ' + idx2token(nbest_hyps_id_offline[0][0]))

                    # TODO(hirofumi): second pass rescoring here

                    # print('*' * 100)

                    # pick up the best hyp
                    best_hyp_id_stream.extend(best_hyp_id_prefix)

                    # reset
                    eout_chunks = []
                    ctc_probs_chunks = []

                # next chunk will start from the frame next to the boundary
                if 0 <= boundary_offset * factor < cs_l - 1:
                    t -= x_chunk[boundary_offset * factor + 1:cs_l].shape[0]

                t += cs_l
                if t >= x_whole.shape[0] - 1:
                    break

            # for the last chunk
            # if len(eout_chunks) > 0:
            #     eout = torch.cat(eout_chunks, dim=1)
            #     elens = torch.IntTensor([eout.size(1)])
            #     nbest_hyps_id_offline, _, _ = self.dec_fwd.beam_search(
            #         eout, elens, global_params, idx2token, lm, lm_2nd, None)
            #     print('MoChA: ' + idx2token(nbest_hyps_id_offline[0][0]))
            #     print('*' * 50)

            return [np.stack(best_hyp_id_stream, axis=0)], [None]

    def decode(self, xs, params, idx2token, nbest=1, exclude_eos=False,
               refs_id=None, refs=None, utt_ids=None, speakers=None,
               task='ys', ensemble_models=[]):
        """Decoding in the inference stage.

        Args:
            xs (list): A list of length `[B]`, which contains arrays of size `[T, input_dim]`
            params (dict): hyper-parameters for decoding
                beam_width (int): the size of beam
                min_len_ratio (float):
                max_len_ratio (float):
                len_penalty (float): length penalty
                cov_penalty (float): coverage penalty
                cov_threshold (float): threshold for coverage penalty
                lm_weight (float): the weight of RNNLM score
                resolving_unk (bool): not used (to make compatible)
                fwd_bwd_attention (bool):
            idx2token (): converter from index to token
            nbest (int):
            exclude_eos (bool): exclude <eos> from best_hyps_id
            refs_id (list): gold token IDs to compute log likelihood
            refs (list): gold transcriptions
            utt_ids (list):
            speakers (list):
            task (str): ys* or ys_sub1* or ys_sub2*
            ensemble_models (list): list of Speech2Text classes
        Returns:
            best_hyps_id (list): A list of length `[B]`, which contains arrays of size `[L]`
            aws (list): A list of length `[B]`, which contains arrays of size `[L, T, n_heads]`

        """
        self.eval()
        with torch.no_grad():
            if task.split('.')[0] == 'ys':
                dir = 'bwd' if self.bwd_weight > 0 and params['recog_bwd_attention'] else 'fwd'
            elif task.split('.')[0] == 'ys_sub1':
                dir = 'fwd_sub1'
            elif task.split('.')[0] == 'ys_sub2':
                dir = 'fwd_sub2'
            else:
                raise ValueError(task)

            # Encode input features
            if self.input_type == 'speech' and self.mtl_per_batch and 'bwd' in dir:
                eout_dict = self.encode(xs, task, flip=True)
            else:
                eout_dict = self.encode(xs, task, flip=False)

            #########################
            # CTC
            #########################
            if (self.fwd_weight == 0 and self.bwd_weight == 0) or (self.ctc_weight > 0 and params['recog_ctc_weight'] == 1):
                lm = getattr(self, 'lm_' + dir, None)
                lm_2nd = getattr(self, 'lm_2nd', None)
                lm_2nd_rev = None  # TODO

                best_hyps_id = getattr(self, 'dec_' + dir).decode_ctc(
                    eout_dict[task]['xs'], eout_dict[task]['xlens'], params, idx2token,
                    lm, lm_2nd, lm_2nd_rev, nbest, refs_id, utt_ids, speakers)
                return best_hyps_id, None

            #########################
            # Attention
            #########################
            else:
                if params['recog_beam_width'] == 1 and not params['recog_fwd_bwd_attention']:
                    best_hyps_id, aws = getattr(self, 'dec_' + dir).greedy(
                        eout_dict[task]['xs'], eout_dict[task]['xlens'],
                        params['recog_max_len_ratio'], idx2token,
                        exclude_eos,  params['recog_oracle'],
                        refs_id, utt_ids, speakers)
                else:
                    assert params['recog_batch_size'] == 1

                    ctc_log_probs = None
                    if params['recog_ctc_weight'] > 0:
                        ctc_log_probs = self.dec_fwd.ctc_log_probs(eout_dict[task]['xs'])

                    # forward-backward decoding
                    if params['recog_fwd_bwd_attention']:
                        lm_fwd = getattr(self, 'lm_fwd', None)
                        lm_bwd = getattr(self, 'lm_bwd', None)

                        # ensemble (forward)
                        ensmbl_eouts_fwd = []
                        ensmbl_elens_fwd = []
                        ensmbl_decs_fwd = []
                        if len(ensemble_models) > 0:
                            for i_e, model in enumerate(ensemble_models):
                                enc_outs_e_fwd = model.encode(xs, task, flip=False)
                                ensmbl_eouts_fwd += [enc_outs_e_fwd[task]['xs']]
                                ensmbl_elens_fwd += [enc_outs_e_fwd[task]['xlens']]
                                ensmbl_decs_fwd += [model.dec_fwd]
                                # NOTE: only support for the main task now

                        # forward decoder
                        nbest_hyps_id_fwd, aws_fwd, scores_fwd = self.dec_fwd.beam_search(
                            eout_dict[task]['xs'], eout_dict[task]['xlens'],
                            params, idx2token, lm_fwd, None, lm_bwd, ctc_log_probs,
                            params['recog_beam_width'], False, refs_id, utt_ids, speakers,
                            ensmbl_eouts_fwd, ensmbl_elens_fwd, ensmbl_decs_fwd)

                        # ensemble (backward)
                        ensmbl_eouts_bwd = []
                        ensmbl_elens_bwd = []
                        ensmbl_decs_bwd = []
                        if len(ensemble_models) > 0:
                            for i_e, model in enumerate(ensemble_models):
                                if self.input_type == 'speech' and self.mtl_per_batch:
                                    enc_outs_e_bwd = model.encode(xs, task, flip=True)
                                else:
                                    enc_outs_e_bwd = model.encode(xs, task, flip=False)
                                ensmbl_eouts_bwd += [enc_outs_e_bwd[task]['xs']]
                                ensmbl_elens_bwd += [enc_outs_e_bwd[task]['xlens']]
                                ensmbl_decs_bwd += [model.dec_bwd]
                                # NOTE: only support for the main task now
                                # TODO(hirofumi): merge with the forward for the efficiency

                        # backward decoder
                        flip = False
                        if self.input_type == 'speech' and self.mtl_per_batch:
                            flip = True
                            enc_outs_bwd = self.encode(xs, task, flip=True)
                        else:
                            enc_outs_bwd = eout_dict
                        nbest_hyps_id_bwd, aws_bwd, scores_bwd, _ = self.dec_bwd.beam_search(
                            enc_outs_bwd[task]['xs'], eout_dict[task]['xlens'],
                            params, idx2token, lm_bwd, None, lm_fwd, ctc_log_probs,
                            params['recog_beam_width'], False, refs_id, utt_ids, speakers,
                            ensmbl_eouts_bwd, ensmbl_elens_bwd, ensmbl_decs_bwd)

                        # forward-backward attention
                        best_hyps_id = fwd_bwd_attention(
                            nbest_hyps_id_fwd, aws_fwd, scores_fwd,
                            nbest_hyps_id_bwd, aws_bwd, scores_bwd,
                            flip, self.eos, params['recog_gnmt_decoding'], params['recog_length_penalty'],
                            idx2token, refs_id)
                        aws = None
                    else:
                        # ensemble
                        ensmbl_eouts, ensmbl_elens, ensmbl_decs = [], [], []
                        if len(ensemble_models) > 0:
                            for i_e, model in enumerate(ensemble_models):
                                if model.input_type == 'speech' and model.mtl_per_batch and 'bwd' in dir:
                                    enc_outs_e = model.encode(xs, task, flip=True)
                                else:
                                    enc_outs_e = model.encode(xs, task, flip=False)
                                ensmbl_eouts += [enc_outs_e[task]['xs']]
                                ensmbl_elens += [enc_outs_e[task]['xlens']]
                                ensmbl_decs += [getattr(model, 'dec_' + dir)]
                                # NOTE: only support for the main task now

                        lm = getattr(self, 'lm_' + dir, None)
                        lm_2nd = getattr(self, 'lm_2nd', None)
                        lm_2nd_rev = getattr(self, 'lm_bwd' if dir == 'fwd' else 'lm_bwd', None)

                        nbest_hyps_id, aws, scores = getattr(self, 'dec_' + dir).beam_search(
                            eout_dict[task]['xs'], eout_dict[task]['xlens'],
                            params, idx2token, lm, lm_2nd, lm_2nd_rev, ctc_log_probs,
                            nbest, exclude_eos, refs_id, utt_ids, speakers,
                            ensmbl_eouts, ensmbl_elens, ensmbl_decs)

                        if nbest == 1:
                            best_hyps_id = [hyp[0] for hyp in nbest_hyps_id]
                        else:
                            return nbest_hyps_id, aws, scores
                        # NOTE: nbest >= 2 is used for MWER training only

                return best_hyps_id, aws
