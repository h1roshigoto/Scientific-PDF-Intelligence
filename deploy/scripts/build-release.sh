#!/bin/sh
set -eu

destination=${1:?Specify an existing artifact directory}
revision=$(git rev-parse HEAD)
epoch=$(git show -s --format=%ct HEAD)
{
    docker version
    docker buildx version
    docker buildx inspect --bootstrap
    printf 'SOURCE_DATE_EPOCH=%s\n' "$epoch"
} > "$destination/toolchain.txt"
for image in runtime proxy; do
    case "$image" in
        runtime) dockerfile=Dockerfile.api ;;
        proxy) dockerfile=Dockerfile.frontend ;;
    esac
    docker buildx build --platform linux/amd64 --provenance=mode=min \
        --label "org.opencontainers.image.revision=$revision" \
        --build-arg "SOURCE_DATE_EPOCH=$epoch" --file "$dockerfile" \
        --output "type=oci,dest=$destination/$image.tar,name=spi-$image:local,rewrite-timestamp=true" .
done
