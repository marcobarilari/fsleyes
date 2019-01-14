#!/bin/bash

set -e

source /test.venv/bin/activate

pip install wheel
python setup.py sdist
python setup.py bdist_wheel

PIPARGS="--retries 10 --timeout 30"

# install dev versions of core
# dependencies, unless we're
# on a release branch or tag
if [[ "$CI_PROJECT_PATH" != "$UPSTREAM_PROJECT"  || "$CI_COMMIT_REF_NAME" == "master" ]]; then
  wget https://git.fmrib.ox.ac.uk/fsl/fslpy/-/archive/master/fslpy-master.tar.bz2
  wget https://git.fmrib.ox.ac.uk/fsl/fsleyes/widgets/-/archive/master/widgets-master.tar.bz2
  wget https://git.fmrib.ox.ac.uk/fsl/fsleyes/props/-/archive/master/props-master.tar.bz2
  tar xf props-master.tar.bz2   && pushd props-master   && pip install $PIPARGS . && popd
  tar xf fslpy-master.tar.bz2   && pushd fslpy-master   && pip install $PIPARGS . && popd
  tar xf widgets-master.tar.bz2 && pushd widgets-master && pip install $PIPARGS . && popd
fi


pip install dist/*.whl
pip uninstall -y fsleyes

pip install dist/*.tar.gz
pip uninstall -y fsleyes
