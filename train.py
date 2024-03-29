import argparse
import logging 
import os
import cv2
import numpy as np
import tensorflow as tf
from tensorflow import keras as keras
from keras import backend as K
from keras import utils
from model.network import Glcic
from model.config import Config
from model import util
import dataset

tf.compat.v1.disable_eager_execution()
print("\n\neager?")
print(tf.executing_eagerly())
class PrintAccuracy(keras.callbacks.Callback):
    """
    訓練中にモデル内部の状態と統計量を可視化する
    
    Attributes
    ----------
    keras.callbacks.Callback
        コールバックは訓練中で適用される関数集合で、訓練中にモデル内部の状態と統計量を可視化するときに使用する
    """
    def __init__(self,logger,**kwargs):
        """
        Parameters
        ----------
        logger :logging.Logger
            ソフトウェア実行時のイベントを追跡するモジュール
        **kwargs :(?)
        """
        super().__init__(**kwargs)
        self.logger =logger
    def on_batch_end(self,batch,logs={}):
        self.logger.info("accuracy: %s",logs.get('acc'))
class SaveGeneratorOutput(keras.callbacks.Callback):
    def __init__(self, data_generator, batch_size, tests, **kwargs):
        super().__init__(**kwargs)
        self.data_generator = data_generator
        self.batch_size = batch_size
        self.tests = tests

    def on_epoch_end(self, epoch, logs={}):
        outputs = self.model.predict(self.tests, batch_size=self.batch_size,
                                     verbose=1)
        if not isinstance(outputs, list):
            outputs = [outputs]
        for output in outputs:
            if len(output.shape) == 4 and output.shape[3] == 3:
                # おそらく画像
                output = np.split(output, output.shape[0], axis=0)
                for i, image in enumerate(output):
                    image = np.squeeze(image, 0)
                    image = self.data_generator.denormalize_image(image)
                    cv2.imwrite('./out/epoch{}_{}.png'.format(epoch, i), image)
if __name__ == "__main__":
    FORMAT = '%(asctime)-15s %(levelname)s #[%(thread)d] %(message)s'
    logging.basicConfig(format=FORMAT, level=logging.INFO)

    logger = logging.getLogger(__name__)
    logger.info("---start---")
    # GPUのメモリを必要な分だけ確保する
    physical_devices = tf.config.experimental.list_physical_devices('GPU')
    if len(physical_devices) >0:
        for k in range(len(physical_devices)):
            tf.config.experimental.set_memory_growth(physical_devices[k], True)
            print('memory growth:', tf.config.experimental.get_memory_growth(physical_devices[k]))
    else:
        print("Not enough GPU hardware devices available")
    config = Config()    
    argparser = argparse.ArgumentParser(
        description="Globally and Locally Consistent Image Completion(GLCIC)"
        + " - train model.")
    argparser.add_argument('--data_dir', type=str,
                        required=True, help="データセット配置ディレクトリ." +
                        "data_dir/train, data_dir/valの両方がある想定.")
    argparser.add_argument('--stage', type=int,
                        required=True,
                        help="トレーニングステージ.1:generator only, " +
                        "2:discriminator only, 3:all",
                        choices=[1, 2, 3])
    argparser.add_argument('--weights_path', type=str,
                        required=False, help="モデルの重みファイルのパス")
    argparser.add_argument('--testimage_path', type=str,
                        required=False, help="epoch毎にpredictする画像が" +
                        "格納されたディレクトリ.格納されている画像数はバッチサイズと" +
                        "同じであること。")
    args = argparser.parse_args()
    logger.info("args: %s", args)
    
    # GPU数やバッチ数を指定。複数GPUでの動作は未検証
    config.gpu_num = 1
    config.batch_size = 16
    util.out_name_pattern = "(.+_loss$|.+_debug$)"

    #学習モデル
    network = Glcic(batch_size=config.batch_size, input_shape=config.input_shape,
                    mask_shape=config.mask_shape)

    train_generator = True
    train_discriminator = True
    if args.stage == 1:
        # generatorのみ訓練
        model, base_model = network.compile_generator(
            gpu_num=config.gpu_num,
            learning_rate=config.learning_rate)
        train_discriminator = False
        steps_per_epoch = 100
        epochs = 100  # batch_size(16) * 100 * 100 iterations per stage
        print(type(model))
    logger.info("train_generator:%s, train_discriminator:%s, "
                + "steps_per_epoch:%s, epochs:%s",
                train_generator, train_discriminator, steps_per_epoch, epochs)
    if args.weights_path:
        # 重みがあればロード
        logger.info("load weight:%s", args.weights_path)
        model.load_weights(args.weights_path, by_name=True)
        logger.info(model.summary())
    # ネットワーク構成を画像として保存
    utils.plot_model(model, './model.png', True, True)
     # レイヤの重み更新有無を確認
    for i, layer in enumerate(model.layers):
        if layer.__class__.__name__ == 'TimeDistributed':
            name = layer.layer.name
            trainable = layer.layer.trainable
        else:
            name = layer.name
            trainable = layer.trainable
        logger.info('%s %s:%s', i, name, trainable)
     # 学習、検証データ生成器の準備
    gen = dataset.DataGenerator(config)
    train_data_generator = gen.generate(
        os.path.join(args.data_dir, "train"), train_generator, train_discriminator)
    val_data_generator = gen.generate(
        os.path.join(args.data_dir, "val"), train_generator, train_discriminator)
    model_file_path = './nnmodel/glcic-stage{}-{}'.format(
        args.stage, '{epoch:02d}-{val_loss:.2f}.h5')
    callbacks = [keras.callbacks.TerminateOnNaN(),
                keras.callbacks.TensorBoard(log_dir='./tb_log',
                                            histogram_freq=0,
                                            write_graph=True,
                                            write_images=False),
                # keras.callbacks.ReduceLROnPlateau(monitor='val_loss',
                #                                   verbose=1,
                #                                   factor=0.7,
                #                                   patience=10,
                #                                   min_lr=config.learning_rate
                #                                   / 30)
                keras.callbacks.ModelCheckpoint(filepath=model_file_path,
                                                verbose=1,
                                                save_weights_only=True,
                                                save_best_only=False,
                                                period=20)]
    if args.testimage_path and not args.stage == 2:
        # epoch毎にgeneratorの出力を保存
        test_data_generator = gen.generate(args.testimage_path,
                                        train_generator, train_discriminator)
        inputs, _ = next(test_data_generator)
        callbacks.append(SaveGeneratorOutput(gen, config.batch_size, inputs))
    
    