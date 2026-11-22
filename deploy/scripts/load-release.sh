#!/bin/sh
set -eu

source=${1:?Specify an OCI artifact directory}
revision=$(git rev-parse HEAD)
for image in runtime proxy; do
    tag="spi-$image:local"
    docker load -i "$source/$image.tar"
    expected=$(PYTHONPATH=src python3 -c 'import sys; from pathlib import Path; from spi.ops.release import oci_subject
print(oci_subject(Path(sys.argv[1]), sys.argv[2])["config_digest"])' "$source/$image.tar" "$revision")
    test "$(docker image inspect --format '{{.Id}}' "$tag")" = "$expected"
done
