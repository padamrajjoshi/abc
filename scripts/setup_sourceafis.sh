#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
command -v java >/dev/null || { echo 'Install Java 11 or later first.' >&2; exit 1; }
command -v mvn >/dev/null || { echo 'Install Maven first.' >&2; exit 1; }
mvn -B -f vendor/sourceafis/pom.xml org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies -DoutputDirectory="$(pwd)/vendor/sourceafis"
java -version
printf '%s\n' 'SourceAFIS runtime ready. Set MATCHER_ENGINE=sourceafis to require it.'
