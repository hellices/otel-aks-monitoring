# 모니터링: Prometheus / Grafana 방식 (myapp)

> OTel Operator auto-inject → App Pod에서 직접 Prometheus `/metrics` 노출 → PodMonitor → ama-metrics → Managed Prometheus → Grafana
>
> 공통 인프라(OTel Operator, AMPLS)는 [monitoring.md](monitoring.md), Application Insights 방식은 [monitoring-appinsights.md](monitoring-appinsights.md) 참조.

---

## 1. 아키텍처

```
App Pod (agui-server, ns: myapp)
  │
  │  ① OTel auto-instrumentation (init container 주입)
  │     + OpenAI instrumentor (gen_ai.* 메트릭)
  │     + FastAPI instrumentor (http_server.* 메트릭)
  │     + 커스텀 메트릭 (agui.* 메트릭)
  │     + Prometheus exporter (:9464 /metrics)
  │
  │  ② PodMonitor 직접 스크래핑 (30s 간격)
  ▼
ama-metrics (kube-system, Managed Prometheus addon)
  │
  │  ③ Private Link (AMPLS + PE)
  ▼
Azure Monitor Workspace → Managed Grafana
```

| 항목 | 값 |
|---|---|
| App Namespace | `myapp` |
| 메트릭 포트 | `:9464` (Prometheus exporter, OTel SDK 내장) |
| 트레이스 | 미수집 (`OTEL_TRACES_EXPORTER=none`) |
| OTel Collector | **불필요** (앱이 직접 /metrics 노출) |
| 인증 | DCR/DCE 자동 (ama-metrics addon) |
| 확인 도구 | Azure Managed Grafana |

---

## 2. 앱 자동 계측 (Python)

### 2.1 Instrumentation CR

매니페스트: [`myapp/otel-instrumentation.yaml`](../myapp/otel-instrumentation.yaml)

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: python-instrumentation
  namespace: myapp
spec:
  propagators: [tracecontext, baggage]
  python:
    env:
      - name: OTEL_METRICS_EXPORTER
        value: prometheus
      - name: OTEL_EXPORTER_PROMETHEUS_PORT
        value: "9464"
      - name: OTEL_EXPORTER_PROMETHEUS_HOST
        value: "0.0.0.0"
      - name: OTEL_TRACES_EXPORTER
        value: none
      - name: OTEL_LOGS_EXPORTER
        value: none
  resource:
    resourceAttributes:
      service.name: agui-server
      service.namespace: myapp
      deployment.environment: production
```

```bash
kubectl apply -f myapp/otel-instrumentation.yaml
```

### 2.2 PodMonitor

> ⚠️ API Group이 `azmonitoring.coreos.com` (Azure 전용). 표준 `monitoring.coreos.com`이 아님.

PodMonitor는 `k8s-deploy.yaml`에 포함되어 있으며, 앱 Pod의 `:9464/metrics`를 직접 스크래핑:

```yaml
apiVersion: azmonitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: agui-server-metrics
  namespace: myapp
spec:
  selector:
    matchLabels:
      app: agui-server
  podMetricsEndpoints:
    - port: metrics       # 9464
      path: /metrics
      interval: 30s
```

### 2.3 Deployment 어노테이션

`k8s-deploy.yaml`의 Pod template에 추가:

```yaml
annotations:
  instrumentation.opentelemetry.io/inject-python: "true"
```

OTel Operator가 자동으로:
- init container (`opentelemetry-auto-instrumentation-python`) 주입
- `OTEL_*` 환경변수 설정 (exporter, service name, resource attributes 등)
- `PYTHONPATH`에 auto-instrumentation 라이브러리 추가

### 2.4 OpenAI Instrumentor + 커스텀 메트릭

`gen_ai.*` 메트릭은 auto-instrumentation만으로는 수집 안 됨. 앱 이미지에 instrumentor를 설치하고 명시적으로 호출해야 함:

```python
# server.py
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from opentelemetry import metrics

OpenAIInstrumentor().instrument()  # gen_ai.* 메트릭 활성화

# 커스텀 메트릭
meter = metrics.get_meter("agui.server", "1.0.0")
agent_request_counter = meter.create_counter(
    "agui.agent.request.count",
    description="Total number of AG-UI agent requests",
    unit="1",
)
```

**requirements.txt 주의사항:**
```
opentelemetry-api
opentelemetry-semantic-conventions-ai==0.4.13   # ⚠️ 0.4.14는 LLM_SYSTEM 제거됨
opentelemetry-instrumentation-openai-v2
```

> `opentelemetry-semantic-conventions-ai`는 반드시 `==0.4.13` 고정. 0.4.14에서 `SpanAttributes.LLM_SYSTEM`이 제거되어 `agent-framework`가 `AttributeError`로 크래시함.

---

## 3. 수집되는 메트릭

| 메트릭 | 타입 | scope | 설명 |
|---|---|---|---|
| `gen_ai_client_operation_duration_seconds` | histogram | `opentelemetry.instrumentation.openai_v2` | Gen AI 호출 latency |
| `agui_agent_request_count_total` | counter | `agui.server` (커스텀) | 에이전트 요청 수 |
| `http_server_active_requests` | gauge | `opentelemetry.instrumentation.fastapi` | 활성 HTTP 요청 수 |
| `http_server_duration_milliseconds` | histogram | `opentelemetry.instrumentation.fastapi` | HTTP 요청 처리 시간 |
| `http_server_response_size_bytes` | histogram | `opentelemetry.instrumentation.fastapi` | HTTP 응답 크기 |

주요 레이블: `gen_ai_request_model="gpt-5.2-chat"`, `gen_ai_system="openai"`, `server_address="aif-contoso-krc-01.openai.azure.com"`, `service_name="agui-server"`

---

## 4. Grafana PromQL 예시

```promql
# Gen AI 평균 응답 시간
rate(gen_ai_client_operation_duration_seconds_sum[5m])
  / rate(gen_ai_client_operation_duration_seconds_count[5m])

# AG-UI 에이전트 요청 RPS
rate(agui_agent_request_count_total[5m])

# HTTP 요청 처리 시간
rate(http_server_duration_milliseconds_sum[5m])
  / rate(http_server_duration_milliseconds_count[5m])

# 활성 요청 수
http_server_active_requests{service_name="agui-server"}
```

---

## 5. 진단 명령어

```bash
# Instrumentation CR 확인
kubectl get instrumentation -n myapp

# PodMonitor 확인
kubectl get podmonitor -n myapp

# 앱 Pod에 주입된 init container 확인
kubectl get pod -n myapp -l app=agui-server -o jsonpath='{.items[0].spec.initContainers[*].name}'

# 앱 Pod의 OTEL 환경변수 확인
kubectl get pod -n myapp -l app=agui-server -o jsonpath='{range .items[0].spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep OTEL

# 앱 Prometheus 메트릭 직접 확인 (Pod에서 :9464/metrics)
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl \
  -n myapp \
  --overrides='{"spec":{"tolerations":[{"key":"workload","operator":"Equal","value":"general","effect":"NoSchedule"}]}}' \
  -- sh -c 'curl -s http://agui-server.myapp.svc:9464/metrics | grep -E "^(gen_ai|agui|http_server)" | head -20'

# ama-metrics 에러 로그
kubectl exec -n kube-system deploy/ama-metrics -- cat /opt/microsoft/linuxmonagent/mdsd.err

# ama-metrics 정상 로그
kubectl exec -n kube-system deploy/ama-metrics -- cat /opt/microsoft/linuxmonagent/mdsd.info | tail -30
```

---

## 6. 정상 동작 체크리스트

| # | 항목 | 확인 방법 | 기대 결과 |
|---|------|----------|----------|
| 1 | Instrumentation CR 존재 | `kubectl get instrumentation -n myapp` | python-instrumentation |
| 2 | init container 주입됨 | Pod spec 확인 | `opentelemetry-auto-instrumentation-python` |
| 3 | PodMonitor 존재 | `kubectl get podmonitor -n myapp` | agui-server-metrics |
| 4 | 앱 /metrics 응답 | `curl :9464/metrics` | gen_ai, http_server 메트릭 |
| 5 | ama-metrics 실행 | `kubectl get po -n kube-system -l rsName=ama-metrics` | Running |
| 6 | mdsd.err 에러 없음 | `kubectl exec ... cat mdsd.err` | 에러 없음 |
| 7 | Grafana에서 조회 | PromQL 실행 | 데이터 표시 |

---

## 7. 트러블슈팅

### 7.1 PodMonitor 스크래핑 안 됨

```
❌ 증상: PodMonitor가 앱 Pod을 스크래핑하지 못함
```

**원인:** Deployment에 `metrics` 포트(9464)가 정의되지 않았거나, PodMonitor의 `port` 이름이 불일치
**해결:** Deployment `containerPort`에 `name: metrics`가 있는지 확인, PodMonitor `port: metrics`와 매칭

### 7.2 opentelemetry-semantic-conventions-ai 0.4.14 크래시

```
❌ 증상: Pod CrashLoopBackOff
   AttributeError: type object 'SpanAttributes' has no attribute 'LLM_SYSTEM'
```

**원인:** `opentelemetry-instrumentation-openai-v2`가 의존성으로 `0.4.14`를 설치, `agent-framework`가 `SpanAttributes.LLM_SYSTEM` 사용 → 0.4.14에서 제거됨
**해결:** `requirements.txt`에 `opentelemetry-semantic-conventions-ai==0.4.13` 고정
