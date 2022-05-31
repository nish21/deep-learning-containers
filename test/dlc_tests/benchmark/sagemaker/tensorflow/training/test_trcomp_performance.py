import pytest, re
import numpy as np

import boto3, sagemaker
from sagemaker.tensorflow import TensorFlow
from sagemaker.training_compiler.config import TrainingCompilerConfig

from src.benchmark_metrics import (
    TRCOMP_THRESHOLD,
    get_threshold_for_image,
)
from test.test_utils import (
    BENCHMARK_RESULTS_S3_BUCKET,
    LOGGER,
    get_framework_and_version_from_tag,
    get_cuda_version_from_tag,
)



@pytest.fixture
def num_gpus(instance_type):
    if instance_type in [
            'ml.p3.2xlarge',
            'ml.g4dn.xlarge',
            'ml.g4dn.2xlarge',
            'ml.g4dn.4xlarge',
            'ml.g4dn.8xlarge',
            'ml.g4dn.16xlarge',
            'ml.g5.xlarge',
            'ml.g5.2xlarge',
            'ml.g5.4xlarge',
            'ml.g5.8xlarge',
            'ml.g5.16xlarge',
            ]:
        return 1
    elif instance_type in [
            'ml.p3.16xlarge',
            'ml.p3dn.24xlarge',
            'ml.g5.48xlarge',
            ]:
        return 8
    else:
        raise ValueError(f'Unforeseen instance {instance_type}')


@pytest.fixture
def total_n_gpus(num_gpus, instance_count):
    return num_gpus*instance_count


@pytest.fixture
def distribution_strategy(instance_type,  num_gpus, instance_count, request):
    if instance_count==1:
        if num_gpus==1:
            request.applymarker(pytest.mark.one_device)
            return 'one_device'
        else:
            request.applymarker(pytest.mark.mirrored)
            return 'mirrored'
    else:
        request.applymarker(pytest.mark.multi_worker_mirrored)
        return 'multi_worker_mirrored'


@pytest.fixture
def caching(instance_type,  num_gpus, instance_count, distribution_strategy):
    if distribution_strategy in [
                                'one_device',
                                ]:
        return False
    else:
        return True


@pytest.fixture
def sagemaker_session(region):
    return sagemaker.Session(boto_session=boto3.Session(region_name=region))


@pytest.fixture
def framework_version(tensorflow_training):
    _, version = get_framework_and_version_from_tag(tensorflow_training)
    return version


@pytest.fixture(autouse=True)
def smtrcomp_only(framework_version, tensorflow_training, request):
    short_version = float(".".join(framework_version.split('.')[:2]))
    if short_version<2.9:
        pytest.skip('Training Compiler support was added with TF 2.9.0')
    if 'gpu' not in tensorflow_training:
        pytest.skip('Training Compiler is only available for GPUs')



def pytest_generate_tests(metafunc):
    if "instance_type" in metafunc.fixturenames:
        metafunc.parametrize("instance_type, instance_count", [
                                                pytest.param('ml.p3.2xlarge', 1, marks=[pytest.mark.p3, pytest.mark.single_gpu]),
                                                # pytest.param('ml.p3.16xlarge', 1, marks=[pytest.mark.p3, pytest.mark.single_node_multi_gpu]),
                                                # pytest.param('ml.p3dn.24xlarge', 2, marks=[pytest.mark.p3, pytest.mark.multi_node_multi_gpu]),
                                                ])



@pytest.mark.flaky(reruns=1)
@pytest.mark.usefixtures("sagemaker_only")
class TestImageClassification:


    @pytest.mark.model("resnet101")
    def test_resnet101_at_fp16(self, instance_type, num_gpus, total_n_gpus, instance_count, distribution_strategy, caching, tensorflow_training, sagemaker_session, capsys, framework_version):
        epochs = int(100*total_n_gpus)
        batches = np.array([224])*total_n_gpus
        for batch in np.array(batches, dtype=int):
            train_steps = int(10240*epochs/batch)
            steps_per_loop = train_steps//10
            overrides=\
            f"runtime.enable_xla=True,"\
            f"runtime.num_gpus={num_gpus},"\
            f"runtime.distribution_strategy={distribution_strategy},"\
            f"runtime.mixed_precision_dtype=float16,"\
            f"task.train_data.global_batch_size={batch},"\
            f"task.train_data.input_path=/opt/ml/input/data/training/validation*,"\
            f"task.train_data.cache={caching},"\
            f"trainer.train_steps={train_steps},"\
            f"trainer.steps_per_loop={steps_per_loop},"\
            f"trainer.summary_interval={steps_per_loop},"\
            f"trainer.checkpoint_interval={train_steps},"\
            f"task.model.backbone.type=resnet,"\
            f"task.model.backbone.resnet.model_id=101"
            estimator = TensorFlow(
                                sagemaker_session=sagemaker_session,
                                git_config={
                                    'repo': 'https://github.com/tensorflow/models.git',
                                    'branch': 'v2.9.2',
                                },
                                source_dir='.',
                                entry_point='official/vision/train.py',
                                model_dir=False,
                                instance_type=instance_type,
                                instance_count=instance_count,
                                image_uri=tensorflow_training,
                                hyperparameters={
                                    TrainingCompilerConfig.HP_ENABLE_COMPILER : True,
                                    'experiment': 'resnet_imagenet',
                                    'config_file' : 'official/vision/configs/experiments/image_classification/imagenet_resnet50_gpu.yaml',
                                    'mode' : 'train',
                                    'model_dir': '/opt/ml/model',
                                    'params_override' : overrides,
                                },
                                debugger_hook_config=None,
                                disable_profiler=True,
                                max_run=60*60*1, # Timeout in 1 hours
                                base_job_name=f"tf{framework_version.replace('.','')}-trcomp-bench-resnet101",
                                role="SageMakerRole",
                            )
            estimator.fit(inputs='s3://collection-of-ml-datasets/Imagenet/TFRecords/validation', logs=True, wait=True)
            
            captured = capsys.readouterr()
            logs = captured.out + captured.err
            match = re.search('Billable seconds: ([0-9]*)', logs)
            billable = int(match.group(1))

            threshold = TRCOMP_THRESHOLD['tensorflow']['2.9']['resnet101'][instance_type][instance_count][batch]
            result = (
                f"tensorflow-trcomp {framework_version} resnet101 fp16 XLA "
                f"imagenet {instance_type} {instance_count} {batch} Billable: {billable} secs threshold: {threshold} secs "
                f"{estimator.latest_training_job.name}"
            )
            LOGGER.info(result)
            assert billable>=1000, 'False Positive '+result
            assert billable<=threshold, result

