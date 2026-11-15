#!/usr/bin/env bash
# 端到端演示脚本（在宿主机直接运行，无需 Docker）。
# 前置：已创建 venv 并安装依赖、Playwright Chromium 及系统库。
# 用法：./demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${VENV:-$ROOT/../.venv}"
DATA="$(mktemp -d)/calib-demo"
mkdir -p "$DATA"
PORT=18080
APPPORT=18000
export DATA_DIR="$DATA"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/tmp/pw}"
export PORTAL_INTERNAL="http://127.0.0.1:$PORT"
export PORTAL_EXTERNAL="http://localhost:$PORT"
# 无 root 本地环境补充的 Chromium 系统库（容器内不需要）
if [ -d /tmp/syslibs/root ]; then
  export LD_LIBRARY_PATH="/tmp/syslibs/root/usr/lib/x86_64-linux-gnu:/tmp/syslibs/root/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

cleanup() {
  echo; echo "== 清理演示进程 =="
  kill "${PORTAL_PID:-}" "${APP_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT

echo "== 1. 启动模拟旧门户（端口 $PORT）与填报助手（端口 $APPPORT）=="
PORTAL_DB="$DATA/portal.db" "$VENV/bin/python" -m uvicorn \
  --app-dir "$ROOT/portal" app:app --host 127.0.0.1 --port "$PORT" \
  >"$DATA/portal.log" 2>&1 &
PORTAL_PID=$!
"$VENV/bin/python" -m uvicorn --app-dir "$ROOT" app.webapp:app \
  --host 127.0.0.1 --port "$APPPORT" >"$DATA/app.log" 2>&1 &
APP_PID=$!
sleep 10
curl -fsS "http://127.0.0.1:$PORT/" -o /dev/null
curl -fsS "http://127.0.0.1:$APPPORT/" -o /dev/null
echo "   门户: http://localhost:$PORT  助手: http://localhost:$APPPORT"

echo "== 2. 上传示例 CSV（含同值跳过 / 异值暂停 / 多条暂停三种种子情形）=="
curl -fsS -F "portal_url=http://127.0.0.1:$PORT" \
  -F "file=@$ROOT/data/sample.csv" \
  "http://127.0.0.1:$APPPORT/api/batches" | head -c 300; echo
sleep 25
echo "   批次 1 逐行状态："
curl -fsS "http://127.0.0.1:$APPPORT/api/batches/1/rows" \
  | "$VENV/bin/python" -c "import sys,json;d=json.load(sys.stdin);[print(f\"   - {r['device_no']} {r['cal_date']} {r['indication_error']}: {r['status']}\") for r in d['rows']]"

echo "== 3. 演示保存后断线：门户注入 drop_submit 故障后提交新行 =="
curl -fsS -X POST -d "mode=drop_submit" "http://127.0.0.1:$PORT/admin/fault" -o /dev/null
printf '设备编号,校准日期,示值误差\nEQ-9001,2026-11-20,+0.77\n' > "$DATA/drop.csv"
curl -fsS -F "portal_url=http://127.0.0.1:$PORT" -F "file=@$DATA/drop.csv" \
  "http://127.0.0.1:$APPPORT/api/batches" >/dev/null
sleep 20
curl -fsS "http://127.0.0.1:$APPPORT/api/batches/2/rows" \
  | "$VENV/bin/python" -c "import sys,json;r=json.load(sys.stdin)['rows'][0];print('   断线行结果:',r['status'],'-',r['detail'])"

echo "== 4. 演示查询失败暂停：注入 query_500 =="
curl -fsS -X POST -d "mode=query_500" "http://127.0.0.1:$PORT/admin/fault" -o /dev/null
printf '设备编号,校准日期,示值误差\nEQ-9002,2026-11-21,-0.13\n' > "$DATA/q500.csv"
curl -fsS -F "portal_url=http://127.0.0.1:$PORT" -F "file=@$DATA/q500.csv" \
  "http://127.0.0.1:$APPPORT/api/batches" >/dev/null
sleep 15
curl -fsS "http://127.0.0.1:$APPPORT/api/batches/3/rows" \
  | "$VENV/bin/python" -c "import sys,json;r=json.load(sys.stdin)['rows'][0];print('   查询失败行结果:',r['status'],'-',r['detail'])"

echo "== 5. 演示重启续办：杀掉助手进程后重启，在途/暂停行只查询不重提 =="
kill -9 "$APP_PID"; APP_PID=""
sleep 2
"$VENV/bin/python" -m uvicorn --app-dir "$ROOT" app.webapp:app \
  --host 127.0.0.1 --port "$APPPORT" >>"$DATA/app.log" 2>&1 &
APP_PID=$!
sleep 15
echo "   重启后各批次状态："
curl -fsS "http://127.0.0.1:$APPPORT/api/batches/1/rows" \
  | "$VENV/bin/python" -c "import sys,json;d=json.load(sys.stdin);print('   批次1:',d['batch']['status'],'-',d['batch']['note'])"

echo
echo "演示完成。浏览器打开："
echo "  填报助手  http://localhost:$APPPORT"
echo "  门户后台  http://localhost:$PORT/admin/records"
echo "数据目录: $DATA"
wait
