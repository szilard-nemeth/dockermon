#!/usr/bin/env bash

echo "Replacing config values from environment..."
echo "Printing env:"
env
envsubst < /config.yml > /tmp-config.yml
RET_CODE=$?

if [ "$RET_CODE" == "0" ]; then
    mv /tmp-config.yml /config.yml
    echo "Config file contents after replacements:"
    cat /config.yml
fi