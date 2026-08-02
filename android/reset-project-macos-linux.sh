#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
rm -rf .idea .gradle build app/build
printf '%s\n' 'Project caches removed.' 'Reopen this folder in Android Studio and select JDK 17.'
