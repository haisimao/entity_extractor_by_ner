# -*- coding: utf-8 -*-
# @Time : 2020/9/13 3:18 下午
# @Author : lishouxian
# @Email : gzlishouxian@gmail.com
# @File : train.py
# @Software: PyCharm
import tensorflow as tf
import numpy as np
import math
import time
from tqdm import tqdm
from engines.model import NerModel
from engines.utils.metrics import metrics
from tensorflow_addons.text.crf import crf_decode
from tensorflow_addons.optimizers import AdamW


def train(configs, data_manager, logger):
    vocab_size = data_manager.max_token_number
    num_classes = data_manager.max_label_number
    learning_rate = configs.learning_rate
    max_to_keep = configs.checkpoints_max_to_keep
    checkpoints_dir = configs.checkpoints_dir
    checkpoint_name = configs.checkpoint_name
    best_f1_val = 0.0
    best_at_epoch = 0
    unprocessed = 0
    very_start_time = time.time()
    epoch = configs.epoch
    batch_size = configs.batch_size

    if configs.optimizer == 'Adagrad':
        optimizer = tf.keras.optimizers.Adagrad(learning_rate=learning_rate)
    elif configs.optimizer == 'Adadelta':
        optimizer = tf.keras.optimizers.Adadelta(learning_rate=learning_rate)
    elif configs.optimizer == 'RMSprop':
        optimizer = tf.keras.optimizers.RMSprop(learning_rate=learning_rate)
    elif configs.optimizer == 'SGD':
        optimizer = tf.keras.optimizers.SGD(learning_rate=learning_rate)
    elif configs.optimizer == 'Adam':
        optimizer = tf.keras.optimizers.Adam(learning_rate=learning_rate)
    else:
        optimizer = AdamW(learning_rate=learning_rate, weight_decay=1e-2)

    pretrained_model = None
    if configs.use_pretrained_model and not configs.finetune:
        if configs.pretrained_model == 'Bert':
            from transformers import TFBertModel
            pretrained_model = TFBertModel.from_pretrained('bert-base-chinese')

    train_dataset, val_dataset = data_manager.get_training_set()
    ner_model = NerModel(configs, vocab_size, num_classes)

    checkpoint = tf.train.Checkpoint(ner_model=ner_model)
    checkpoint_manager = tf.train.CheckpointManager(
        checkpoint, directory=checkpoints_dir, checkpoint_name=checkpoint_name, max_to_keep=max_to_keep)
    checkpoint.restore(checkpoint_manager.latest_checkpoint)
    if checkpoint_manager.latest_checkpoint:
        print('Restored from {}'.format(checkpoint_manager.latest_checkpoint))
    else:
        print('Initializing from scratch.')

    num_val_iterations = int(math.ceil(1.0 * len(val_dataset) / batch_size))
    logger.info(('+' * 20) + 'training starting' + ('+' * 20))
    for i in range(epoch):
        start_time = time.time()
        logger.info('epoch:{}/{}'.format(i + 1, epoch))
        for step, batch in tqdm(train_dataset.shuffle(len(train_dataset)).batch(batch_size).enumerate()):
            if configs.use_pretrained_model:
                X_train_batch, y_train_batch, att_mask_batch = batch
                if configs.finetune:
                    # 如果微调
                    model_inputs = (X_train_batch, att_mask_batch)
                else:
                    # 不进行微调，预训练模型只做特征的增强
                    model_inputs = pretrained_model(X_train_batch, attention_mask=att_mask_batch)[0]
            else:
                X_train_batch, y_train_batch = batch
                model_inputs = X_train_batch
            # 计算没有加入pad之前的句子的长度
            inputs_length = tf.math.count_nonzero(X_train_batch, 1)
            with tf.GradientTape() as tape:
                logits, log_likelihood, transition_params = ner_model(
                    inputs=model_inputs, inputs_length=inputs_length, targets=y_train_batch, training=1)
                loss = -tf.reduce_mean(log_likelihood)
            # 定义好参加梯度的参数
            variables = ner_model.trainable_variables
            # 将预训练模型里面的pooler层的参数去掉
            variables = [var for var in variables if 'pooler' not in var.name]
            gradients = tape.gradient(loss, variables)

            if configs.use_gan:
                if configs.gan_method == 'fgm':
                    # 使用FGM的对抗办法
                    epsilon = 1.0
                    embedding = variables[0]
                    embedding_gradients = gradients[0]
                    embedding_gradients = tf.zeros_like(embedding) + embedding_gradients
                    delta = epsilon * embedding_gradients / tf.norm(embedding_gradients, ord=2)

                    accum_vars = [tf.Variable(tf.zeros_like(grad), trainable=False) for grad in gradients]
                    gradients = [accum_vars[i].assign_add(grad) for i, grad in enumerate(gradients)]
                    variables[0].assign_add(delta)

                    with tf.GradientTape() as gan_tape:
                        logits, log_likelihood, _ = ner_model(inputs=model_inputs, inputs_length=inputs_length,
                                                 targets=y_train_batch, training=1)
                        loss = -tf.reduce_mean(log_likelihood)
                    gan_gradients = gan_tape.gradient(loss, variables)
                    gradients = [gradients[i].assign_add(grad) for i, grad in enumerate(gan_gradients)]
                    variables[0].assign_sub(delta)

                elif configs.gan_method == 'pgd':
                    # 使用PGD的对抗办法
                    K = 3
                    alpha = 0.3
                    epsilon = 1
                    origin_embedding = tf.Variable(variables[0])
                    accum_vars = [tf.Variable(tf.zeros_like(grad), trainable=False) for grad in gradients]
                    origin_gradients = [accum_vars[i].assign_add(grad) for i, grad in enumerate(gradients)]

                    for t in range(K):
                        embedding = variables[0]
                        embedding_gradients = gradients[0]
                        embedding_gradients = tf.zeros_like(embedding) + embedding_gradients
                        delta = alpha * embedding_gradients / tf.norm(embedding_gradients, ord=2)
                        variables[0].assign_add(delta)

                        r = variables[0] - origin_embedding
                        if tf.norm(r, ord=2) > epsilon:
                            r = epsilon * r / tf.norm(r, ord=2)
                        variables[0].assign(origin_embedding + tf.Variable(r))

                        if t != K - 1:
                            gradients = [tf.Variable(tf.zeros_like(grad), trainable=False) for grad in gradients]
                        else:
                            gradients = origin_gradients
                        with tf.GradientTape() as gan_tape:
                            logits, log_likelihood, _ = ner_model(inputs=model_inputs, inputs_length=inputs_length,
                                                                  targets=y_train_batch, training=1)
                            loss = -tf.reduce_mean(log_likelihood)
                        gan_gradients = gan_tape.gradient(loss, variables)
                        gradients = [gradients[i].assign_add(grad) for i, grad in enumerate(gan_gradients)]
                    variables[0].assign(origin_embedding)

            # 反向传播，自动微分计算
            optimizer.apply_gradients(zip(gradients, variables))
            if step % configs.print_per_batch == 0 and step != 0:
                batch_pred_sequence, _ = crf_decode(logits, transition_params, inputs_length)
                measures, _ = metrics(
                    X_train_batch, y_train_batch, batch_pred_sequence, configs, data_manager)
                res_str = ''
                for k, v in measures.items():
                    res_str += (k + ': %.3f ' % v)
                logger.info('training batch: %5d, loss: %.5f, %s' % (step, loss, res_str))

        # validation
        logger.info('start evaluate engines...')
        loss_values = []
        val_results = {}
        val_labels_results = {}
        for label in data_manager.suffix:
            val_labels_results.setdefault(label, {})
        for measure in configs.measuring_metrics:
            val_results[measure] = 0
        for label, content in val_labels_results.items():
            for measure in configs.measuring_metrics:
                if measure != 'accuracy':
                    val_labels_results[label][measure] = 0

        for val_batch in tqdm(val_dataset.batch(batch_size)):
            if configs.use_pretrained_model:
                X_val_batch, y_val_batch, att_mask_batch = val_batch
                if configs.finetune:
                    model_inputs = (X_val_batch, att_mask_batch)
                else:
                    model_inputs = pretrained_model(X_val_batch, attention_mask=att_mask_batch)[0]
            else:
                X_val_batch, y_val_batch = val_batch
                model_inputs = X_val_batch
            inputs_length_val = tf.math.count_nonzero(X_val_batch, 1)
            logits_val, log_likelihood_val, transition_params_val = ner_model(
                inputs=model_inputs, inputs_length=inputs_length_val, targets=y_val_batch)
            val_loss = -tf.reduce_mean(log_likelihood_val)
            batch_pred_sequence_val, _ = crf_decode(logits_val, transition_params_val, inputs_length_val)
            measures, lab_measures = metrics(
                X_val_batch, y_val_batch, batch_pred_sequence_val, configs, data_manager)

            for k, v in measures.items():
                val_results[k] += v
            for lab in lab_measures:
                for k, v in lab_measures[lab].items():
                    val_labels_results[lab][k] += v
            loss_values.append(val_loss)

        time_span = (time.time() - start_time) / 60
        val_res_str = ''
        val_f1_avg = 0
        for k, v in val_results.items():
            val_results[k] /= num_val_iterations
            val_res_str += (k + ': %.3f ' % val_results[k])
            if k == 'f1':
                val_f1_avg = val_results[k]
        for label, content in val_labels_results.items():
            val_label_str = ''
            for k, v in content.items():
                val_labels_results[label][k] /= num_val_iterations
                val_label_str += (k + ': %.3f ' % val_labels_results[label][k])
            logger.info('label: %s, %s' % (label, val_label_str))
        logger.info('time consumption:%.2f(min), %s' % (time_span, val_res_str))

        if np.array(val_f1_avg).mean() > best_f1_val:
            unprocessed = 0
            best_f1_val = np.array(val_f1_avg).mean()
            best_at_epoch = i + 1
            checkpoint_manager.save()
            logger.info('saved the new best model with f1: %.3f' % best_f1_val)
        else:
            unprocessed += 1

        if configs.is_early_stop:
            if unprocessed >= configs.patient:
                logger.info('early stopped, no progress obtained within {} epochs'.format(configs.patient))
                logger.info('overall best f1 is {} at {} epoch'.format(best_f1_val, best_at_epoch))
                logger.info('total training time consumption: %.3f(min)' % ((time.time() - very_start_time) / 60))
                return
    logger.info('overall best f1 is {} at {} epoch'.format(best_f1_val, best_at_epoch))
    logger.info('total training time consumption: %.3f(min)' % ((time.time() - very_start_time) / 60))
