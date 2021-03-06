

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from os.path import normpath, basename
import pickle
from datetime import datetime
import config as CONFIG
import snli_reader
import tensorflow as tf
from epoch import run_epoch, bucket_shuffle
from Vocab import Vocab
from IAAModel import IAAModel
from DAModel import DAModel
from AdaptiveAnalysisModel import AdaptiveAnalysisModel
from ACTAttnAnalysisModel import ActAttnAnalysisModel
from ACTDAAnalysisModel import ACTDAAnalysisModel
from embedding_utils import import_embeddings
import saveload
import argparse
import numpy as np
import time
from collections import defaultdict

def analysis_epoch(session, models, data, vocab):
    """Runs the model on the given data."""
    #epoch_size = ((len(data) // m.batch_size) - 1) // m.num_steps
    start_time = time.time()
    costs = 0.0
    iters = 0
    accuracy = 0.0
    return_data = []
    epoch_size, id_to_data = bucket_shuffle(data)

    for step, (id,(x, y)) in enumerate(id_to_data):
        m = models[id]
        assert x["premise"].shape == (m.premise.get_shape())
        assert x["hypothesis"].shape == (m.hypothesis.get_shape())

        batch_acc, cost, probs, prem, hyp, act_step_accs, act_step_dist = session.run([m.accuracy, m.cost, m.ACTPROB, m.ACTPREMISEATTN,m.ACTHYPOTHESISATTN, m.per_step_accs, m.per_step_dists], feed_dict={m.premise: x["premise"],
                                      m.hypothesis: x["hypothesis"],
                                      m.targets: y})
        print(act_step_accs)
        print(act_step_dist)
        stats = {}
        stats["per_step_accs"] = act_step_accs
        stats["per_step_dist"] = act_step_dist.squeeze(1)
        stats["act_probs"] = probs.squeeze(1)
        stats["premise"] = vocab.tokens_for_ids(x["premise"].squeeze().tolist())
        stats["premise_attention"] = prem.squeeze(1)
        stats["hypothesis"] = vocab.tokens_for_ids(x["hypothesis"].squeeze().tolist())
        stats["hypothesis_attention"] = hyp.squeeze(1)
        stats["correct"] = batch_acc
        stats["class"] = np.argmax(y.squeeze())
        return_data.append(stats)

        costs += cost
        iters += 1
        accuracy += batch_acc
        #if verbose and step % (epoch_size // 10) == 10:
        print("%.3f acc: %.3f loss: %.3f speed: %.0f examples/s" %
              (step * 1.0 / epoch_size,
               accuracy / iters,
               costs / iters,
               iters * m.batch_size / (time.time() - start_time)))


    return (costs / iters), (accuracy / iters), return_data

def get_config_and_model(conf):

    if conf == "DAModel":
        return DAModel, CONFIG.DAConfig()
    elif conf == "IAAModel":
        return IAAModel, CONFIG.IAAConfig()
    elif conf == "AdaptiveIAAModel":
        return AdaptiveAnalysisModel, CONFIG.AdaptiveIAAConfig()
    elif conf == "ACTAttentionModel":
        return ActAttnAnalysisModel, CONFIG.ACTAttentionConfig()
    elif conf == "ACTDAModel":
        return ACTDAAnalysisModel, CONFIG.ACTDAConfig()

def main(unused_args):
    vocab_path = args.vocab_path
    weights_dir = args.weights_dir
    verbose = args.verbose
    debug = args.debug

    MODEL, _ = get_config_and_model(args.model)

    stats_from_training = pickle.load(open(weights_dir + "/stats.pkl", "rb"))
    eval_config = stats_from_training["eval_config"][0]

    saved_model_path = stats_from_training['test_file'][0]

    if weights_dir is not None:
        if not os.path.exists(weights_dir):
            os.mkdir(weights_dir)

    vocab = Vocab(vocab_path, args.data_path,max_vocab_size=40000)

    eval_config.batch_size = 1

    train, val, test = "snli_1.0_train.jsonl","snli_1.0_dev.jsonl","snli_1.0_test.jsonl"

    # Dataset Stats(we ignore bad labels)
    #                   train   val    test
    # max hypothesis:   82      55     30
    # max premise:      62      59     57
    # bad labels        785     158    176

    if debug:
        buckets = [(15,10)]
        raw_data = snli_reader.load_data(args.data_path,train, val, test, vocab, False,
                            max_records=1000,buckets=buckets, batch_size=eval_config.batch_size)
    else:
        buckets = [(10,5),(20,10),(30,20),(40,30),(50,40),(82,62)]
        raw_data = snli_reader.load_data(args.data_path,train, val, test, vocab, False,
                            max_records=None, buckets=buckets, batch_size=eval_config.batch_size)

    _,_, test_data, stats = raw_data
    print(stats)
    test_buckets = {x:v for x,v in enumerate(test_data)}


    if eval_config.use_embeddings:
        embedding_var = np.random.normal(0.0, eval_config.init_scale, [eval_config.vocab_size, eval_config.embedding_size])
    else:
        embedding_var = None

    print("loading models into memory")
    trainingStats = defaultdict(list)
    trainingStats["eval_config"].append(eval_config)
    with tf.Graph().as_default(), tf.Session() as session:
        initialiser = tf.random_uniform_initializer(-eval_config.init_scale, eval_config.init_scale)

        # generate a model per bucket which share parameters
        # store in list where index corresponds to the id of the batch
        with tf.variable_scope("all_models_with_buckets"):
            models_test = []

            for i in range(len(test_buckets)):
                # correct config for this bucket
                eval_config.prem_steps, eval_config.hyp_steps = buckets[i]
                eval_config.prem_steps, eval_config.hyp_steps = buckets[i]

                with tf.variable_scope('model', reuse= True if i > 0 else None, initializer=initialiser):

                    models_test.append(MODEL(eval_config, pretrained_embeddings=embedding_var,
                                             update_embeddings=eval_config.train_embeddings, is_training=False))
                    #### Reload Model ####
                    if saved_model_path is not None:

                        v_dic = {v.name: v for v in tf.trainable_variables()}
                        loaded_weights = pickle.load(open(weights_dir + saved_model_path, "rb"))

                        for key, value in loaded_weights.items():

                            session.run(tf.assign(v_dic[key], value))

            test_loss, test_acc, processed_data = analysis_epoch(session, models_test, test_buckets,vocab)

            trainingStats["test_loss"].append(test_loss)
            trainingStats["test_acc"].append(test_acc)
            print("Test Accuracy:", test_acc)
            print("Test Loss:", test_loss)

            param_path = basename(normpath(weights_dir))
            pickle.dump([eval_config,processed_data], open(os.path.join(weights_dir, param_path + "_config_processed_data.pkl"), "wb"))
            if verbose:
                print("Test Accuracy: {}".format(test_acc))


if __name__ == '__main__':

    parser = argparse.ArgumentParser()

    parser.add_argument("--model")
    parser.add_argument("--data_path")
    parser.add_argument("--weights_dir")
    parser.add_argument("--verbose")
    parser.add_argument("--debug", action='store_true', default=False)
    parser.add_argument("--vocab_path")

    from sys import argv

    args = parser.parse_args()
    main(argv)
