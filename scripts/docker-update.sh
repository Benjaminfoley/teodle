#!/bin/sh

docker run --privileged --rm tonistiigi/binfmt --install all && \
docker buildx build --platform linux/amd64 -t docker.monicz.pl/teodle --push .
