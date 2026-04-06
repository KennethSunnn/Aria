# ARIA 运维健康检查

在 Web 服务已启动（默认 `http://localhost:5000`）的前提下，可用下列命令快速检查 API 与子系统。将端口改为你实际监听的地址即可。

## 服务与 API Key

```bash
curl -s http://localhost:5000/api/check_api_key 2>/dev/null && echo "ARIA 服务运行中" || echo "ARIA 服务未响应"
```

## 方法论库健康度

```bash
echo "--- 方法论健康度 ---"
curl -s http://localhost:5000/api/methodology_health 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    total = d.get('total', 0)
    stable = d.get('stable_count', 0)
    caution = d.get('caution_count', 0)
    deprecate = d.get('deprecate_count', 0)
    print(f'总计: {total} | 稳定: {stable} | 注意: {caution} | 待废弃: {deprecate}')
except Exception:
    print('无法获取方法论健康度（服务可能未运行）')
" 2>/dev/null
```

## KAIROS 状态

```bash
echo "--- KAIROS 状态 ---"
curl -s http://localhost:5000/api/kairos/status 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    running = d.get('running', False)
    triggers = d.get('trigger_count', 0)
    last_dream = d.get('last_dream', '从未')
    print(f'运行中: {running} | 触发器数量: {triggers} | 上次 AutoDream: {last_dream}')
except Exception:
    print('KAIROS 未启用或接口不可用（可设置 ARIA_KAIROS_ENABLED=1）')
" 2>/dev/null
```

## 记忆子系统状态

```bash
echo "--- 记忆状态 ---"
curl -s http://localhost:5000/api/get_memory_status 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(json.dumps(d, ensure_ascii=False, indent=2))
except Exception:
    print('无法获取记忆状态')
" 2>/dev/null
```
