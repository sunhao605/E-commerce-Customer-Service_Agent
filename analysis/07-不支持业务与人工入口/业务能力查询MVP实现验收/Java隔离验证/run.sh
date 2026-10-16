#!/bin/sh
set -eu
mkdir -p /out/.runtime/classes
cp -R /src/src/main/resources/. /out/.runtime/classes/
find /src/src/main/java -name '*.java' | sort > /out/.runtime/sources.txt
printf '%s\n' /out/CapabilityIsolationCheck.java >> /out/.runtime/sources.txt
java -version > /out/java-version.log 2>&1
javac --release 17 -parameters -encoding UTF-8 -cp '/lib-deps/*' -d /out/.runtime/classes @/out/.runtime/sources.txt > /out/compile.log 2>&1
java -cp '/out/.runtime/classes:/lib-deps/*' verification.CapabilityIsolationCheck /out > /out/runtime.log 2>&1
