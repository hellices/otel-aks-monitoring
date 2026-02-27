# 모니터링: Application Insights 방식 (otel-app)

> 앱 내장 OTel SDK → OTel Collector → `azuremonitor` exporter → Application Insights (traces + metrics)
>
> 공통 인프라(OTel Operator, AMPLS)는 [monitoring.md](monitoring.md), Prometheus/Grafana 방식은 [monitoring-prometheus.md](monitoring-prometheus.md) 참조.

---

## 1. 아키텍처

```
App Pod (question-app, ns: otel-app)
  │
  │  ① 앱 내장 OTel SDK
  │     (opentelemetry-sdk + opentelemetry-exporter-otlp-proto-grpc)
  │
  │  ② OTLP gRPC (:4317)
  ▼
OTel Collector (ns: otel-app)
  │  receivers: otlp
  │  processors: batch (1024/5s)
  │  exporters: azuremonitor (traces + metrics)
  │
  └── azuremonitor exporter ── AMPLS Private Link ──┐
                                                ▼
                                  Application Insights
                                  ├─ Transaction Search (트레이스)
                                  ├─ Application Map (의존성)
                                  └─ Metrics Explorer (메트릭)
```

| 항목 | 값 |
|---|---|
| App Namespace | `otel-app` |
| Collector Namespace | `otel-app` (앱과 동일) |
| Collector 이미지 | `otel-collector-contrib:0.146.1` |
| App 이미지 | [`ghcr.io/hellices/otel-langfuse:sha-a190823`](https://github.com/hellices/otel-langfuse) |
| OTLP 프로토콜 | gRPC (:4317) |
| 트레이스 | `azuremonitor` → Application Insights |
| 메트릭 | `azuremonitor` → Application Insights |
| 인증 | Application Insights Connection String (Secret) |
| 확인 도구 | Application Insights |

---

## 2. 사전 준비: Connection String Secret

Collector가 Azure Monitor로 데이터를 전송하려면 Application Insights connection string이 필요.

매니페스트: `infra/azure-monitor-secret.yaml` (`.gitignore` 대상 — 커밋되지 않음)

```bash
kubectl apply -f infra/azure-monitor-secret.yaml
```

**Secret 구조:**
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: azure-monitor-secret
  namespace: otel-app
type: Opaque
stringData:
  connection-string: "InstrumentationKey=<key>;IngestionEndpoint=https://<region>.in.applicationinsights.azure.com/;..."
```

> ⚠️ AMPLS 환경에서는 `IngestionEndpoint` 도메인이 Private DNS Zone (`privatelink.monitor.azure.com`)을 통해 Private IP로 해석되므로, Firewall TLS Inspection을 우회하여 직접 통신됨.

---

## 3. OTel Collector 구성

매니페스트: [`infra/otel-collector.yaml`](../infra/otel-collector.yaml)

```bash
kubectl apply -f infra/azure-monitor-secret.yaml   # Secret 먼저
kubectl apply -f infra/otel-collector.yaml
```

### 3.1 Collector config

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }
processors:
  batch: { send_batch_size: 1024, timeout: 5s }
exporters:
  azuremonitor:
    connection_string: ${env:APPLICATIONINSIGHTS_CONNECTION_STRING}
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [azuremonitor]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [azuremonitor]
```

**핵심 특징:**
- `azuremonitor` exporter만 사용 — Connection String으로 App Insights에 직접 전송
- traces + metrics 모두 App Insights로 전송
- Prometheus exporter / PodMonitor 등 불필요한 이중 경로 제거

### 3.2 환경변수 주입

Collector Pod에 connection string을 Secret에서 주입:

```yaml
env:
  - name: APPLICATIONINSIGHTS_CONNECTION_STRING
    valueFrom:
      secretKeyRef:
        name: azure-monitor-secret
        key: connection-string
```

### 3.3 Service 확인

```bash
kubectl get svc -n otel-app -l app.kubernetes.io/managed-by=opentelemetry-operator
# otel-collector-collector             ClusterIP  ...  4317,4318
# otel-collector-collector-monitoring  ClusterIP  ...  8888
```

---

## 4. 앱 계측

`question-app`은 [`hellices/otel-langfuse`](https://github.com/hellices/otel-langfuse) 이미지로, OTel Operator auto-inject를 사용하지 **않음**. 앱 내부에서 OTel SDK로 직접 OTLP gRPC를 전송.

### 4.1 Deployment 환경변수

```yaml
# otel-app/deployment.yaml
env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-collector-collector.otel-app.svc.cluster.local:4317"
```

- 앱 내부에서 `opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-grpc` 사용
- Instrumentation CR이나 `inject-python` 어노테이션 불필요
- 앱이 직접 trace span을 생성하고 metric을 기록

### 4.2 Prometheus 방식과의 차이

| 항목 | Prometheus 방식 (myapp) | Application Insights 방식 (otel-app) |
|---|---|---|
| 계측 방법 | OTel Operator Instrumentation CR + auto-inject | 앱 내장 OTel SDK |
| OTLP 프로토콜 | 없음 (앱이 직접 Prometheus 노출) | gRPC (:4317) |
| Collector | 불필요 | `otel-app` 네임스페이스 |
| 어노테이션 | `instrumentation.opentelemetry.io/inject-python: "true"` | 없음 |
| init container | `opentelemetry-auto-instrumentation-python` 자동 주입 | 없음 |
| 환경변수 설정 | OTel Operator Webhook이 자동 주입 | Deployment에서 직접 설정 |

---

## 5. Application Insights에서 확인

### 5.1 Transaction Search

Azure Portal → Application Insights → Transaction Search
- 개별 요청의 전체 트레이스 확인
- 요청-응답 시간, 상태 코드, 의존성 호출 추적

### 5.2 Application Map

Azure Portal → Application Insights → Application Map
- 서비스 간 의존성 토폴로지 시각화
- question-app → Azure OpenAI 호출 관계 확인

### 5.3 Metrics Explorer

Azure Portal → Application Insights → Metrics
- 커스텀 메트릭을 Custom Metric Namespace에서 조회
- 차트 생성 및 알림 설정 가능

---

## 6. 진단 명령어

```bash
# Collector 상태
kubectl get po -n otel-app -l app.kubernetes.io/component=opentelemetry-collector

# Collector 로그 (azuremonitor exporter 전송 확인)
kubectl logs -n otel-app -l app.kubernetes.io/component=opentelemetry-collector --tail=50

# question-app 상태
kubectl get po -n otel-app -l app=question-app
kubectl logs -n otel-app -l app=question-app -f

# Connection String Secret 확인
kubectl get secret azure-monitor-secret -n otel-app -o jsonpath='{.data.connection-string}' | base64 -d
```

---

## 7. 정상 동작 체크리스트

| # | 항목 | 확인 방법 | 기대 결과 |
|---|------|----------|----------|
| 1 | Collector 실행 | `kubectl get po -n otel-app -l app.kubernetes.io/component=opentelemetry-collector` | Running |
| 2 | question-app 실행 | `kubectl get po -n otel-app -l app=question-app` | Running |
| 3 | Connection String Secret 존재 | `kubectl get secret azure-monitor-secret -n otel-app` | 존재 |
| 4 | Collector 로그에 에러 없음 | `kubectl logs ...` | 에러 없음 |
| 5 | App Insights 트레이스 수신 | Application Insights → Transaction Search | 트레이스 표시 |
| 6 | App Insights 메트릭 수신 | Application Insights → Metrics Explorer | 메트릭 표시 |

---

## 8. 트러블슈팅

### 8.1 Collector에서 Application Insights 전송 실패

```
❌ 증상: Collector 로그에 "failed to push trace data" 또는 connection 에러
```

**원인:** Connection String이 잘못되었거나 AMPLS를 통한 Ingestion Endpoint 접근 불가
**해결:**
1. Secret의 connection string 확인
2. `nslookup <region>.in.applicationinsights.azure.com` → Private IP 반환 여부
3. AMPLS에 Application Insights 리소스가 연결되어 있는지 확인

### 8.2 트레이스는 보이는데 메트릭이 안 보임

```
❌ 증상: Transaction Search에 트레이스는 있지만 Metrics Explorer에 커스텀 메트릭 없음
```

**원인:** 앱이 메트릭을 전송하지 않거나 Collector metrics 파이프라인 설정 누락
**해결:** Collector config에서 `metrics` 파이프라인의 `exporters`에 `azuremonitor`가 포함되어 있는지 확인

