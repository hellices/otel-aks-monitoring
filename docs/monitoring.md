# 모니터링 구축 가이드

> AKS 폐쇄망 환경에서 OTel Operator 기반 자동 계측 + Azure Managed Prometheus + Grafana 메트릭 파이프라인 구축.

---

## 1. 아키텍처

### 1.1 메트릭 파이프라인 (전체 흐름)

```
App Pod (agui-server)
  │
  │  ① OTel auto-instrumentation (init container 주입)
  │     + OpenAI instrumentor (gen_ai.* 메트릭)
  │     + FastAPI instrumentor (http_server.* 메트릭)
  │     + 커스텀 메트릭 (agui.* 메트릭)
  │
  │  ② OTLP HTTP (:4318)
  ▼
OTel Collector (opentelemetry-operator-system)
  │  receivers: otlp
  │  processors: batch (1024/5s)
  │  exporters: prometheus (:8889)
  │
  │  ③ PodMonitor 스크래핑 (30s 간격)
  ▼
ama-metrics (kube-system, Managed Prometheus addon)
  │
  │  ④ Private Link (AMPLS + PE)
  ▼
Azure Monitor Workspace → Managed Grafana
```

### 1.2 설계 선택: ama-metrics 스크래핑 방식

두 가지 옵션을 검토한 후 **Option B**를 선택:

| | Option A: Direct Remote Write | Option B: ama-metrics 스크래핑 ✅ |
|---|---|---|
| 방식 | Collector → prometheusremotewrite → DCE | Collector → prometheus exporter → ama-metrics |
| 장점 | Collector가 직접 전송 | ama-metrics 내장, DCR/DCE 인증 자동 |
| 단점 | DCE 인증 수동 구성 필요 | 스크래핑 간격만큼 지연 |

**선택 이유**: ama-metrics가 AKS에 이미 내장 (Managed Prometheus addon), DCR/DCE 인증 자동 구성, PodMonitor만 추가하면 연동 완료.

---

## 2. OTel Operator 설치

매니페스트: [`infra/opentelemetry-operator.yaml`](../infra/opentelemetry-operator.yaml)

CRD, RBAC, Webhook, cert-manager Certificate/Issuer, Deployment 포함.

```bash
kubectl apply -f infra/opentelemetry-operator.yaml

# 확인
kubectl get po -n opentelemetry-operator-system
# opentelemetry-operator-controller-manager-...  Running
```

| 항목 | 값 |
|---|---|
| Operator 버전 | v0.145.0 |
| 이미지 | `ghcr.io/open-telemetry/opentelemetry-operator/opentelemetry-operator:0.145.0` |
| CRDs | Instrumentation, OpenTelemetryCollector, TargetAllocator, OpAMPBridge |

---

## 3. OTel Collector + PodMonitor 배포

매니페스트: [`infra/otel-collector.yaml`](../infra/otel-collector.yaml)

```bash
kubectl apply -f infra/otel-collector.yaml
```

### 3.1 Collector 구성

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }
processors:
  batch: { send_batch_size: 1024, timeout: 5s }
exporters:
  prometheus:
    endpoint: 0.0.0.0:8889
    resource_to_telemetry_conversion: { enabled: true }
service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus]
```

| 항목 | 값 |
|---|---|
| 이미지 | `otel-collector-contrib:0.146.0` |
| Mode | deployment (replicas: 1) |
| OTLP gRPC | `:4317` |
| OTLP HTTP | `:4318` |
| Prometheus | `:8889` (메트릭 노출) |

### 3.2 PodMonitor

> ⚠️ API Group이 `azmonitoring.coreos.com` (Azure 전용). 표준 `monitoring.coreos.com`이 아님.

```yaml
apiVersion: azmonitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: otel-collector-metrics
  namespace: opentelemetry-operator-system
spec:
  podMetricsEndpoints:
    - port: prometheus    # 앱 메트릭 (OTLP → Prometheus 변환)
      path: /metrics
      interval: 30s
    - port: metrics       # Collector 자체 메트릭 (8888)
      path: /metrics
      interval: 30s
```

### 3.3 Collector Service 확인

```bash
kubectl get svc -n opentelemetry-operator-system -l app.kubernetes.io/managed-by=opentelemetry-operator
# otel-collector-collector             ClusterIP  10.66.236.213  4317,4318,8889
# otel-collector-collector-monitoring  ClusterIP  ...            8888
```

---

## 4. 앱 자동 계측 (Python)

### 4.1 Instrumentation CR

매니페스트: [`myapp/otel-instrumentation.yaml`](../myapp/otel-instrumentation.yaml)

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: python-instrumentation
  namespace: myapp
spec:
  exporter:
    endpoint: http://otel-collector-collector.opentelemetry-operator-system.svc.cluster.local:4318
  propagators: [tracecontext, baggage]
  python:
    env:
      - name: OTEL_METRICS_EXPORTER
        value: otlp          # 메트릭만 수집
      - name: OTEL_TRACES_EXPORTER
        value: none           # 트레이스 비활성화
      - name: OTEL_LOGS_EXPORTER
        value: none           # 로그 비활성화
  resource:
    resourceAttributes:
      service.name: agui-server
      service.namespace: myapp
      deployment.environment: production
```

```bash
kubectl apply -f myapp/otel-instrumentation.yaml
```

### 4.2 Deployment 어노테이션

`k8s-deploy.yaml`의 Pod template에 추가:

```yaml
annotations:
  instrumentation.opentelemetry.io/inject-python: "true"
```

이 어노테이션으로 OTel Operator가 자동으로:
- init container (`opentelemetry-auto-instrumentation-python`) 주입
- `OTEL_*` 환경변수 설정 (exporter, service name, resource attributes 등)
- `PYTHONPATH`에 auto-instrumentation 라이브러리 추가

### 4.3 앱 내 OpenAI Instrumentor + 커스텀 메트릭

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

### 4.4 수집되는 메트릭

| 메트릭 | 타입 | scope | 설명 |
|---|---|---|---|
| `gen_ai_client_operation_duration_seconds` | histogram | `opentelemetry.instrumentation.openai_v2` | Gen AI 호출 latency |
| `agui_agent_request_count_total` | counter | `agui.server` (커스텀) | 에이전트 요청 수 |
| `http_server_active_requests` | gauge | `opentelemetry.instrumentation.fastapi` | 활성 HTTP 요청 수 |
| `http_server_duration_milliseconds` | histogram | `opentelemetry.instrumentation.fastapi` | HTTP 요청 처리 시간 |
| `http_server_response_size_bytes` | histogram | `opentelemetry.instrumentation.fastapi` | HTTP 응답 크기 |

주요 레이블: `gen_ai_request_model="gpt-5.2-chat"`, `gen_ai_system="openai"`, `server_address="aif-contoso-krc-01.openai.azure.com"`, `service_name="agui-server"`

### 4.5 Grafana PromQL 예시

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

## 5. AMPLS (Azure Monitor Private Link Scope)

> 폐쇄망에서 ama-metrics → Azure Monitor 통신 시, Firewall TLS Inspection이 인증서를 변조하여 SSL 에러 발생. AMPLS + Private Endpoint로 VNet 내부에서 직접 통신해야 함.

### 5.1 AMPLS 구성

| 항목 | 값 |
|---|---|
| **이름** | `ampls-contoso-krc-01` |
| **Private Endpoint** | `pe-ampls-contoso-krc-01` (pe-subnet) |

**연결된 리소스 (DCE 2개):**

| DCE | 리소스 그룹 |
|---|---|
| `MSProm-koreacentral-aks-contoso-koreacentral` | `rg-contoso-koreacentral-01` |
| `azuremonitor-workspace-contoso-krc-01` | `MA_..._managed` |

### 5.2 DCR-DCE 연결

Portal → Monitor → 데이터 수집 규칙 → 각 DCR 선택 → 데이터 수집 엔드포인트 → DCE 선택.

### 5.3 configurationAccessEndpoint DCRA (핵심!)

> **이것이 가장 중요한 단계.**
> DCR에 DCE를 연결하는 것만으로는 부족. AKS 리소스에 `configurationAccessEndpoint` DCRA를 추가해야 에이전트가 Private Link를 통해 구성을 가져올 수 있음.

**DCRA란?**
- DCR/DCE를 **특정 리소스(AKS)에 바인딩**하는 연결 객체
- `configurationAccessEndpoint` DCRA가 없으면 → 에이전트가 어떤 DCE를 써야 하는지 모름 → **403 거부**

**설정 방법 (포털):**
1. Monitor → 데이터 수집 규칙 → Prometheus DCR 선택
2. 리소스 탭 → AKS 리소스의 **데이터 수집 엔드포인트** 열에서 DCE 선택
3. 저장 → `configurationAccessEndpoint` DCRA 자동 생성

**설정 방법 (REST API):**
```bash
az rest --method put \
  --url "/subscriptions/f752aff6-b20c-4973-b32b-0a60ba2c6764/resourceGroups/rg-contoso-koreacentral-01/providers/Microsoft.ContainerService/managedClusters/aks-contoso-koreacentral-01/providers/Microsoft.Insights/dataCollectionRuleAssociations/configurationAccessEndpoint?api-version=2023-03-11" \
  --body '{"properties":{"dataCollectionEndpointId":"/subscriptions/f752aff6-b20c-4973-b32b-0a60ba2c6764/resourceGroups/rg-contoso-koreacentral-01/providers/Microsoft.Insights/dataCollectionEndpoints/MSProm-koreacentral-aks-contoso-koreacentral"}}'
```

**AKS에 걸린 DCRA 3개 (정상 상태):**

| DCRA | 역할 | 연결 대상 |
|---|---|---|
| `ContainerInsightsExtension` | 컨테이너 로그 수집 | DCR → AKS |
| `ContainerInsightsMetricsExtension` | Prometheus 메트릭 수집 | DCR → AKS |
| **`configurationAccessEndpoint`** | 구성 액세스 엔드포인트 | **DCE → AKS** ✅ |

---

## 6. 트러블슈팅

### 6.1 SSL Handshake 에러

```
❌ 증상: ama-metrics 로그에 "Error in SSL handshake" 반복
```

**원인:** Firewall TLS Inspection이 Azure Monitor 인증서를 변조 → SSL 실패  
**해결:** AMPLS + Private Endpoint 생성 → DNS가 Private IP로 해석 → Firewall 우회

### 6.2 403 "Data collection endpoint must be used"

```
❌ 증상: SSL 해결 후 403 에러
   "Data collection endpoint must be used to access configuration over private link."
```

**원인:** DNS가 Private IP를 반환하면 Azure Monitor가 Private Link 접근으로 인식 → DCE 통한 접근 강제 → `configurationAccessEndpoint` DCRA 없음  
**해결:** AKS에 `configurationAccessEndpoint` DCRA 생성 (위 5.3 참고)

### 6.3 PodMonitor 스크래핑 안 됨

```
❌ 증상: PodMonitor가 Collector를 스크래핑하지 못함
```

**원인:** `port: prometheus-dev` ≠ 실제 포트 이름 `prometheus`  
**해결:** `port: prometheus`로 수정

### 6.4 opentelemetry-semantic-conventions-ai 0.4.14 크래시

```
❌ 증상: Pod CrashLoopBackOff
   AttributeError: type object 'SpanAttributes' has no attribute 'LLM_SYSTEM'
```

**원인:** `opentelemetry-instrumentation-openai-v2`가 의존성으로 `0.4.14`를 설치, `agent-framework`가 `SpanAttributes.LLM_SYSTEM` 사용 → 0.4.14에서 제거됨  
**해결:** `requirements.txt`에 `opentelemetry-semantic-conventions-ai==0.4.13` 고정

### 6.5 PodMonitor 적용 시 Azure RBAC 에러

```
❌ 증상: kubectl apply -f aks/monitor/pod-monitor.yaml 실행 시 Forbidden 에러
   podmonitors.azmonitoring.coreos.com "<name>" is forbidden:
   User "<user>" cannot get resource "podmonitors" in API group "azmonitoring.coreos.com"
   in the namespace "<namespace>": User does not have access to the resource in Azure.
   Update role assignment to allow access.
```

**원인:** AKS에 Azure AD + Azure RBAC 통합이 활성화되어 있으면 Kubernetes 리소스 접근이 Azure RBAC 롤 할당으로 제어됨. `azmonitoring.coreos.com` CRD(PodMonitor, ServiceMonitor)를 생성/수정하려면 네임스페이스 또는 클러스터 수준의 Azure Kubernetes Service RBAC 롤이 필요.

**해결 방법 1 — Azure Portal:**

1. [Azure Portal](https://portal.azure.com) → AKS 클러스터 리소스 선택
2. 왼쪽 메뉴 **액세스 제어(IAM)** 클릭
3. **+ 추가** → **역할 할당 추가** 클릭
4. **역할** 탭에서 `Azure Kubernetes Service RBAC Admin` 검색 후 선택  
   (클러스터 전체 관리자 권한이 필요하면 `Azure Kubernetes Service RBAC Cluster Admin` 선택)
5. **구성원** 탭 → **구성원 선택** → 권한을 부여할 사용자 이메일 입력 후 선택
6. **조건** 탭 → 특정 네임스페이스로 범위를 제한하려면 조건 추가 가능 (선택 사항)
7. **검토 + 할당** 클릭

**해결 방법 2 — az 명령:**

```bash
# 변수 설정
SUBSCRIPTION_ID="<구독 ID>"
RESOURCE_GROUP="<리소스 그룹>"
CLUSTER_NAME="<AKS 클러스터 이름>"
NAMESPACE="<네임스페이스>"        # 네임스페이스 범위로 제한할 경우
ASSIGNEE="<사용자 이메일 또는 Object ID>"

# 방법 A: 클러스터 전체 범위로 Azure Kubernetes Service RBAC Admin 할당
az role assignment create \
  --assignee "${ASSIGNEE}" \
  --role "Azure Kubernetes Service RBAC Admin" \
  --scope "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.ContainerService/managedClusters/${CLUSTER_NAME}"

# 방법 B: 특정 네임스페이스 범위로 제한 (최소 권한 원칙)
az role assignment create \
  --assignee "${ASSIGNEE}" \
  --role "Azure Kubernetes Service RBAC Admin" \
  --scope "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.ContainerService/managedClusters/${CLUSTER_NAME}/namespaces/${NAMESPACE}"

# 할당 확인
az role assignment list \
  --assignee "${ASSIGNEE}" \
  --scope "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.ContainerService/managedClusters/${CLUSTER_NAME}" \
  --output table
```

> **참고:** 역할 할당 후 `az aks get-credentials`로 kubeconfig를 다시 받으면 새 권한이 즉시 반영됨.

```bash
az aks get-credentials \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${CLUSTER_NAME}" \
  --overwrite-existing
```

---

## 7. 진단 명령어

```bash
# OTel Operator 상태
kubectl get po -n opentelemetry-operator-system

# OTel Collector 서비스
kubectl get svc -n opentelemetry-operator-system -l app.kubernetes.io/managed-by=opentelemetry-operator

# Instrumentation CR 확인
kubectl get instrumentation -n myapp

# 앱 Pod에 주입된 init container 확인
kubectl get pod -n myapp -l app=agui-server -o jsonpath='{.items[0].spec.initContainers[*].name}'

# 앱 Pod의 OTEL 환경변수 확인
kubectl get pod -n myapp -l app=agui-server -o jsonpath='{range .items[0].spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep OTEL

# Collector Prometheus 메트릭 직접 확인 (gen_ai, agui, http_server)
kubectl run curl-test --rm -it --restart=Never --image=curlimages/curl \
  -n opentelemetry-operator-system \
  --overrides='{"spec":{"tolerations":[{"key":"workload","operator":"Equal","value":"general","effect":"NoSchedule"}]}}' \
  -- sh -c 'curl -s http://otel-collector-collector:8889/metrics | grep -E "^(gen_ai|agui|http_server)" | head -20'

# ama-metrics 에러 로그
kubectl exec -n kube-system deploy/ama-metrics -- cat /opt/microsoft/linuxmonagent/mdsd.err

# ama-metrics 정상 로그
kubectl exec -n kube-system deploy/ama-metrics -- cat /opt/microsoft/linuxmonagent/mdsd.info | tail -30

# DCRA 목록 확인
az rest --method get \
  --url "/subscriptions/f752aff6-b20c-4973-b32b-0a60ba2c6764/resourceGroups/rg-contoso-koreacentral-01/providers/Microsoft.ContainerService/managedClusters/aks-contoso-koreacentral-01/providers/Microsoft.Insights/dataCollectionRuleAssociations?api-version=2023-03-11"

# DNS Private IP 해석 확인
kubectl exec -n kube-system deploy/ama-metrics -- nslookup <endpoint>

# ama-metrics 재시작
kubectl rollout restart deploy/ama-metrics -n kube-system
kubectl rollout restart ds/ama-metrics-node -n kube-system
```

---

## 8. 정상 동작 체크리스트

| # | 항목 | 확인 방법 | 기대 결과 |
|---|------|----------|----------|
| 1 | OTel Operator 실행 | `kubectl get po -n opentelemetry-operator-system` | Running |
| 2 | OTel Collector 실행 | `kubectl get po -n opentelemetry-operator-system -l app.kubernetes.io/component=opentelemetry-collector` | Running |
| 3 | Instrumentation CR 존재 | `kubectl get instrumentation -n myapp` | python-instrumentation |
| 4 | init container 주입됨 | Pod spec 확인 | `opentelemetry-auto-instrumentation-python` |
| 5 | ama-metrics 실행 | `kubectl get po -n kube-system -l rsName=ama-metrics` | Running |
| 6 | mdsd.err 에러 없음 | `kubectl exec ... cat mdsd.err` | 에러 없음 |
| 7 | DNS Private IP 해석 | `nslookup ...metrics.ingest.monitor.azure.com` | 10.2.0.x |
| 8 | DCRA 3개 존재 | REST API 조회 | configurationAccessEndpoint 포함 |
| 9 | gen_ai 메트릭 수집 | Collector :8889 메트릭 확인 | `gen_ai_client_operation_duration_seconds` |
| 10 | Grafana에서 조회 | PromQL 실행 | 데이터 표시 |
