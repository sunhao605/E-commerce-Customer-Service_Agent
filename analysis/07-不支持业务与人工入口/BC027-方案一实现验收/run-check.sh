#!/bin/bash
# BC-027方案一：隔离编译并运行 InteractionPolicyCheck（内存H2、新输出目录，不覆盖历史after目录）。
set -e
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
OUT="$(cd "$(dirname "$0")" && pwd)"
RUNTIME="$OUT/run"
mkdir -p "$RUNTIME/classes"

docker run --rm --network host \
  -v "$ROOT":/workspace -w /workspace \
  maven:3.9.6-eclipse-temurin-17 bash -c '
set -e
OUT="analysis/07-不支持业务与人工入口/BC027-方案一实现验收"
RUNTIME="$OUT/run"
mkdir -p "$RUNTIME/classes"
cp -r ecommerce-backend/src/main/resources/. "$RUNTIME/classes/"
{
  find ecommerce-backend/src/main/java -name "*.java"
  echo analysis/03-取消与续办/取消意愿隔离验收/IsolatedCancelCheck.java
  echo analysis/03-取消与续办/取消闭环验收/CheckServer.java
  echo "$OUT/InteractionPolicyCheck.java"
} > "$RUNTIME/sources.txt"
javac --release 17 -parameters -encoding UTF-8 \
  -cp "analysis/08-候选订单绑定/候选订单验收/.runtime/lib/*" \
  -d "$RUNTIME/classes" @"$RUNTIME/sources.txt"
echo COMPILE_OK
java -cp "$RUNTIME/classes:analysis/08-候选订单绑定/候选订单验收/.runtime/lib/*" \
  com.teachdemo.ecommerce.customer_service.InteractionPolicyCheck 2>&1 | grep -vE "^WARN|^[0-9]{4}-" | tail -40
' | tee "$RUNTIME/run.log"
