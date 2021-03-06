# coding=utf-8
# Copyright (c) 2021 Ant Group
# Author: Xiang Hu

from typing import List, Tuple
import torch.nn as nn
import torch.nn.functional as F
import torch
from .r2d2_common import CacheSlots
from .r2d2_cuda import R2D2Cuda
from .topdown_parser import TopdownParser
from utils.model_loader import load_model
from .fast_r2d2_inference import force_encode


class FastR2D2Classification(nn.Module):
    def __init__(self, config, label_num, disable_parser=False):
        super().__init__()
        self.r2d2 = R2D2Cuda(config)
        self.parser = TopdownParser(config)
        self.classifier = nn.Sequential(nn.Linear(config.hidden_size, config.intermediate_size),
                                        nn.GELU(),
                                        nn.Dropout(config.hidden_dropout_prob),
                                        nn.Linear(config.intermediate_size, label_num))
        self.disable_parser = disable_parser

    def from_pretrain(self, model_path, parser_path):
        self.r2d2.from_pretrain(model_path)
        load_model(self.parser, parser_path)

    def load_model(self, model_path):
        load_model(self, model_path)
        self.r2d2._tie_weights()

    def forward(self, input_ids: torch.Tensor,
                attention_mask: torch.Tensor,
                num_samples: int = 0,
                atom_spans: List[List[Tuple[int]]] = None,
                labels: torch.Tensor = None,
                force_encoding=False):
        if labels is not None:
            # training
            if not self.disable_parser:
                s_indices = self.parser(input_ids, attention_mask, atom_spans=atom_spans)
            else:
                s_indices = None
            results = self.r2d2(input_ids, attention_mask, merge_trajectories=s_indices,
                                sample_trees=num_samples, recover_tree=True, keep_tensor_cache=True)
            tables = results['tables']
            tensor_cache = results['tensor_cache']
            root_cache_ids = []
            for t in tables:
                root_cache_ids.append(t.root.best_node.cache_id)
            e_ij = tensor_cache.gather(root_cache_ids, [CacheSlots.E_IJ])[0]
            logits = self.classifier(e_ij)
            loss = F.cross_entropy(logits, labels)
            bilm_loss = results['loss']
            if not self.disable_parser:
                sampled_trees = results['sampled_trees']
                kl_loss = self.parser(input_ids, attention_mask,
                                    split_masks=sampled_trees['split_masks'],
                                    split_points=sampled_trees['split_points'])
            else:
                kl_loss = 0
            
            # force encoding
            e_ij = force_encode(self.parser, self.r2d2, input_ids, attention_mask, atom_spans)
            logits = self.classifier(e_ij)
            force_encoding_loss = F.cross_entropy(logits, labels)
            return force_encoding_loss + loss + kl_loss + bilm_loss
        else:
            # Implement two mode for inference
            if not force_encoding:
                if not self.disable_parser:
                    s_indices = self.parser(input_ids, attention_mask, atom_spans=atom_spans)
                else:
                    s_indices = None
                results = self.r2d2(input_ids, attention_mask, merge_trajectories=s_indices,
                                    recover_tree=True, keep_tensor_cache=True, lm_loss=False)
                tables = results['tables']
                tensor_cache = results['tensor_cache']
                root_cache_ids = []
                for t in tables:
                    root_cache_ids.append(t.root.best_node.cache_id)
                e_ij = tensor_cache.gather(root_cache_ids, [CacheSlots.E_IJ])[0]
            else:
                if self.disable_parser:
                    raise Exception('Force encoding is not supported when disable_parser == True')
                e_ij = force_encode(self.parser, self.r2d2, input_ids, attention_mask, atom_spans)
            logits = self.classifier(e_ij)
            return F.softmax(logits, dim=-1)


class FastR2D2CrossSentence(nn.Module):
    def __init__(self, config, label_num, disable_parser=False):
        super().__init__()
        self.r2d2 = R2D2Cuda(config)
        self.parser = TopdownParser(config)
        self.classifier = nn.Sequential(nn.Linear(config.hidden_size, config.intermediate_size),
                                        nn.GELU(),
                                        nn.Dropout(config.hidden_dropout_prob),
                                        nn.Linear(config.intermediate_size, label_num))
        self.task_id = config.pairwise_task_id
        self.disable_parser = disable_parser
    
    def from_pretrain(self, model_path, parser_path):
        self.r2d2.from_pretrain(model_path)
        load_model(self.parser, parser_path)

    def load_model(self, model_path):
        load_model(self, model_path)
        self.r2d2._tie_weights()

    def pairwise_encoding(self, e_ij):
        '''
        e_ij.shape: [batch_size, 2, dim]
        '''
        sz = e_ij.shape[0]
        mask_ids = torch.zeros([
            sz,
        ], dtype=torch.long, device=self.r2d2.device).fill_(self.task_id)
        mask_embedding = self.r2d2.embedding(mask_ids)  # (sz, hidden_dim)
        input_embedding = torch.cat(
            [mask_embedding.unsqueeze(1), e_ij], dim=1)  # (?, 3, dim)
        outputs = self.r2d2.tree_decoder(input_embedding)  # (?, 3, dim)
        mask_hidden = outputs[:, 0, :]  # (?, dim)
        return self.classifier(mask_hidden)

    def forward(self, input_ids: torch.Tensor,
                attention_mask: torch.Tensor,
                num_samples: int = 0,
                atom_spans: List[List[Tuple[int]]] = None,
                labels: torch.Tensor = None,
                force_encoding=False):
        """
        input_ids: shape: [batch_size, 2, max_ids_len]
        attention_mask: [batch_size, 2, max_ids_len]
        """
        input_ids = input_ids.view(-1, input_ids.shape[-1])
        attention_mask = attention_mask.view(-1, attention_mask.shape[-1])
        if labels is not None:
            # training
            if not self.disable_parser:
                s_indices = self.parser(input_ids, attention_mask, atom_spans=atom_spans)
            else:
                s_indices = None
            results = self.r2d2(input_ids, attention_mask, merge_trajectories=s_indices,
                                sample_trees=num_samples, recover_tree=True, keep_tensor_cache=True,
                                lm_loss=True)
            tables = results['tables']
            tensor_cache = results['tensor_cache']
            sampled_trees = results['sampled_trees']
            root_cache_ids = []
            for t in tables:
                root_cache_ids.append(t.root.best_node.cache_id)
            e_ij = tensor_cache.gather(root_cache_ids, [CacheSlots.E_IJ])[0]
            logits = self.pairwise_encoding(e_ij.view(e_ij.shape[0] // 2, 2, e_ij.shape[-1]))
            loss = F.cross_entropy(logits, labels)
            bilm_loss = results['loss']
            kl_loss = self.parser(input_ids, attention_mask,
                                  split_masks=sampled_trees['split_masks'],
                                  split_points=sampled_trees['split_points'])
            
            # force encoding
            e_ij = force_encode(self.parser, self.r2d2, input_ids, attention_mask, atom_spans)
            logits = self.pairwise_encoding(e_ij.view(e_ij.shape[0] // 2, 2, e_ij.shape[-1]))
            force_encoding_loss = F.cross_entropy(logits, labels)
            return force_encoding_loss + loss + kl_loss + bilm_loss
        else:
            # Implement two mode for inference
            if not force_encoding:
                if not self.disable_parser:
                    s_indices = self.parser(input_ids, attention_mask, atom_spans=atom_spans)
                else:
                    s_indices = None
                results = self.r2d2(input_ids, attention_mask, merge_trajectories=s_indices,
                                    recover_tree=True, keep_tensor_cache=True,
                                    lm_loss=False)
                tables = results['tables']
                tensor_cache = results['tensor_cache']
                root_cache_ids = []
                for t in tables:
                    root_cache_ids.append(t.root.best_node.cache_id)
                e_ij = tensor_cache.gather(root_cache_ids, [CacheSlots.E_IJ])[0]
            else:
                if self.disable_parser:
                    raise Exception('Force encoding is not supported when disable_parser == True')
                e_ij = force_encode(self.parser, self.r2d2, input_ids, attention_mask, atom_spans)
            logits = self.pairwise_encoding(e_ij.view(e_ij.shape[0] // 2, 2, e_ij.shape[-1]))
            return F.softmax(logits, dim=-1)