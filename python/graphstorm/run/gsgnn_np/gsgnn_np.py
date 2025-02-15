"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    GSgnn node prediction.
"""

import os
import torch as th
import graphstorm as gs
from graphstorm.config import get_argument_parser
from graphstorm.config import GSConfig
from graphstorm.trainer import GSgnnNodePredictionTrainer
from graphstorm.dataloading import GSgnnNodeTrainData, GSgnnNodeDataLoader
from graphstorm.eval import GSgnnAccEvaluator
from graphstorm.eval import GSgnnRegressionEvaluator
from graphstorm.model.utils import save_embeddings
from graphstorm.model import do_full_graph_inference

def get_evaluator(config):
    if config.task_type == "node_classification":
        return GSgnnAccEvaluator(config.eval_frequency,
                                 config.eval_metric,
                                 config.multilabel,
                                 config.use_early_stop,
                                 config.early_stop_burnin_rounds,
                                 config.early_stop_rounds,
                                 config.early_stop_strategy)
    elif config.task_type == "node_regression":
        return GSgnnRegressionEvaluator(config.eval_frequency,
                                        config.eval_metric,
                                        config.use_early_stop,
                                        config.early_stop_burnin_rounds,
                                        config.early_stop_rounds,
                                        config.early_stop_strategy)
    else:
        raise ValueError("Unknown task type")

def main(args):
    config = GSConfig(args)

    gs.initialize(ip_config=config.ip_config, backend=config.backend)
    train_data = GSgnnNodeTrainData(config.graph_name,
                                    config.part_config,
                                    train_ntypes=config.target_ntype,
                                    node_feat_field=config.node_feat_name,
                                    label_field=config.label_field)
    model = gs.create_builtin_node_gnn_model(train_data.g, config, train_task=True)
    trainer = GSgnnNodePredictionTrainer(model, gs.get_rank(),
                                         topk_model_to_save=config.topk_model_to_save)
    if config.restore_model_path is not None:
        trainer.restore_model(model_path=config.restore_model_path)
    trainer.setup_cuda(dev_id=config.local_rank)
    if not config.no_validation:
        evaluator = get_evaluator(config)
        trainer.setup_evaluator(evaluator)
        assert len(train_data.val_idxs) > 0, "The training data do not have validation set."
        # TODO(zhengda) we need to compute the size of the entire validation set to make sure
        # we have validation data.
    tracker = gs.create_builtin_task_tracker(config, trainer.rank)
    if trainer.rank == 0:
        tracker.log_params(config.__dict__)
    trainer.setup_task_tracker(tracker)
    device = 'cuda:%d' % trainer.dev_id
    dataloader = GSgnnNodeDataLoader(train_data, train_data.train_idxs, fanout=config.fanout,
                                     batch_size=config.batch_size, device=device, train_task=True)
    val_dataloader = None
    test_dataloader = None
    # we don't need fanout for full-graph inference
    fanout = config.eval_fanout if config.use_mini_batch_infer else []
    if len(train_data.val_idxs) > 0:
        val_dataloader = GSgnnNodeDataLoader(train_data, train_data.val_idxs, fanout=fanout,
                                             batch_size=config.eval_batch_size,
                                             device=device, train_task=False)
    if len(train_data.test_idxs) > 0:
        test_dataloader = GSgnnNodeDataLoader(train_data, train_data.test_idxs, fanout=fanout,
                                              batch_size=config.eval_batch_size,
                                              device=device, train_task=False)

    # Preparing input layer for training or inference.
    # The input layer can pre-compute node features in the preparing step if needed.
    # For example pre-compute all BERT embeddings
    model.prepare_input_encoder(train_data)
    if config.save_model_path is not None:
        save_model_path = config.save_model_path
    elif config.save_embed_path is not None:
        # If we need to save embeddings, we need to save the model somewhere.
        save_model_path = os.path.join(config.save_embed_path, "model")
    else:
        save_model_path = None
    trainer.fit(train_loader=dataloader, val_loader=val_dataloader,
                test_loader=test_dataloader, num_epochs=config.num_epochs,
                save_model_path=save_model_path,
                use_mini_batch_infer=config.use_mini_batch_infer,
                save_model_frequency=config.save_model_frequency,
                save_perf_results_path=config.save_perf_results_path,
                freeze_input_layer_epochs=config.freeze_lm_encoder_epochs)

    if config.save_embed_path is not None:
        model = gs.create_builtin_node_gnn_model(train_data.g, config, train_task=False)
        best_model_path = trainer.get_best_model_path()
        # TODO(zhengda) the model path has to be in a shared filesystem.
        model.restore_model(best_model_path)
        # Preparing input layer for training or inference.
        # The input layer can pre-compute node features in the preparing step if needed.
        # For example pre-compute all BERT embeddings
        model.prepare_input_encoder(train_data)
        embeddings = do_full_graph_inference(model, train_data, fanout=config.eval_fanout,
                                             task_tracker=tracker)
        save_embeddings(config.save_embed_path, embeddings, gs.get_rank(),
                        th.distributed.get_world_size(),
                        device=device,
                        node_id_mapping_file=config.node_id_mapping_file)

def generate_parser():
    parser = get_argument_parser()
    return parser

if __name__ == '__main__':
    parser=generate_parser()

    args = parser.parse_args()
    print(args)
    main(args)
