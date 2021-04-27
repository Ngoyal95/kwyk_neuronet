#!/bin/bash

#PBS -l nodes=1:ppn=1,walltime=00:30:00
#PBS -l vmem=4gb
#PBS -N kwyk_neuronet
#SBATCH --gres=gpu:p100:2

set -e
set -x

model=$(jq -r .model config.json)
if [ $model != "bwn" ]; then
    save_var="--save-variance"
fi

rm -rf *.nii.gz #clean output from prevsious run

export SINGULARITYENV_PYTHONNOUSERSITE=true
export SINGULARITYENV_OMP_NUM_THREADS=1
time singularity run --nv -e docker://neuronets/kwyk:version-0.4-gpu \
    -m $model \
    -n $(jq -r .samples config.json) \
    $save_var \
    --save-entropy $(jq -r .t1 config.json) \
    output

mkdir -p parc
mv output_means_orig.nii.gz parc/parc.nii.gz
cp key.txt parc

mkdir -p output 
mv output_entropy_orig.nii.gz output/entropy.nii.gz

mkdir -p parc_fs
mv output_means.nii.gz parc_fs/parc.nii.gz
cp key.txt parc_fs

mkdir -p output_fs
mv output_entropy.nii.gz output_fs/entropy.nii.gz

if [ $model != "bwn" ]; then
    mv output_variance.nii.gz output_fs/variance.nii.gz
    mv output_variance_orig.nii.gz output/variance.nii.gz
fi


