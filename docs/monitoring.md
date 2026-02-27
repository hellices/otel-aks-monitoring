# 모니터링 구축 가이드

> AKS 폐쇄망 환경에서 OTel Operator 기반 두 가지 모니터링 방식을 병행 운영.
> 이 문서는 공통 인프라(OTel Operator, AMPLS)와 전체 개요를 다루며, 각 방식의 상세 구성은 별도 문서에서 설명.

| 방식 | 대상 앱 | 상세 문서 |
|---|---|---|
| **Prometheus / Grafana** | myapp (agui-server) | **[monitoring-prometheus.md](monitoring-prometheus.md)** |
| **Application Insights** | otel-app (question-app) | **[monitoring-appinsights.md](monitoring-appinsights.md)** |

---

## 1. 아키텍처 개요

```
┌─ Prometheus / Grafana 방식 ──────────────────────────────────────────────────┐
│                                                                              │
│  agui-server (myapp)                                                         │
│    │ OTel auto-instrumentation                                               │
│    │ Prometheus exporter (:9464 /metrics)                                     │
│    │                                                                          │
│    └─ PodMonitor ─▶ ama-metrics ─▶ Managed Prometheus ─▶ Grafana             │
└──────────────────────────────────────────────────────────────────────────────┘

┌─ Application Insights 방식 ──────────────────────────────────────────────────┐
│                                                                              │
│  question-app (otel-app)                                                     │
│    │ 앱 내장 OTel SDK ─▶ OTLP gRPC (:4317)                                   │
│    ▼                                                                         │
│  OTel Collector (otel-app)                                                   │
│    └─ azuremonitor exporter ─▶ Application Insights (traces + metrics)       │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 방식 비교

| | Prometheus / Grafana | Application Insights |
|---|---|---|
| **Collector** | 불필요 (앱이 직접 /metrics 노출) | `otel-app` 네임스페이스 |
| **트레이스** | 미수집 | azuremonitor → App Insights |
| **메트릭** | Prometheus (:9464) → PodMonitor → Grafana | azuremonitor → App Insights |
| **앱 계측** | OTel Operator auto-inject | 앱 내장 OTel SDK |
| **OTLP** | 없음 (앱이 직접 Prometheus 노출) | gRPC (:4317) |
| **인증** | DCR/DCE 자동 | Connection String (Secret) |
| **확인 도구** | Grafana | App Insights |

---

## 2. 공통 인프라: OTel Operator

매니페스트: [`infra/opentelemetry-operator.yaml`](../infra/opentelemetry-operator.yaml)

두 방식 모두 OTel Operator가 필요. CRD, RBAC, Webhook, cert-manager Certificate/Issuer, Deployment 포함.

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

## 3. 공통 인프라: AMPLS (Azure Monitor Private Link Scope)

> 폐쇄망에서 Azure Monitor 통신 시, Firewall TLS Inspection이 인증서를 변조하여 SSL 에러 발생.
> AMPLS + Private Endpoint로 VNet 내부에서 직접 통신해야 함.
> 두 방식 모두 AMPLS를 경유 (ama-metrics는 DCE, azuremonitor exporter는 Ingestion Endpoint).

### 3.1 AMPLS 구성

| 항목 | 값 |
|---|---|
| **이름** | `ampls-contoso-krc-01` |
| **Private Endpoint** | `pe-ampls-contoso-krc-01` (pe-subnet) |

**연결된 리소스 (DCE 2개):**

| DCE | 리소스 그룹 |
|---|---|
| `MSProm-koreacentral-aks-contoso-koreacentral` | `rg-contoso-koreacentral-01` |
| `azuremonitor-workspace-contoso-krc-01` | `MA_..._managed` |

### 3.2 DCR-DCE 연결

Portal → Monitor → 데이터 수집 규칙 → 각 DCR 선택 → 데이터 수집 엔드포인트 → DCE 선택.

### 3.3 configurationAccessEndpoint DCRA (핵심!)

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

## 4. 공통 트러블슈팅

> 각 방식별 트러블슈팅은 상세 문서 참조.

### 4.1 SSL Handshake 에러

```
❌ 증상: ama-metrics 로그에 "Error in SSL handshake" 반복
```

**원인:** Firewall TLS Inspection이 Azure Monitor 인증서를 변조 → SSL 실패
**해결:** AMPLS + Private Endpoint 생성 → DNS가 Private IP로 해석 → Firewall 우회

### 4.2 403 "Data collection endpoint must be used"

```
❌ 증상: SSL 해결 후 403 에러
   "Data collection endpoint must be used to access configuration over private link."
```

**원인:** DNS가 Private IP를 반환하면 Azure Monitor가 Private Link 접근으로 인식 → DCE 통한 접근 강제 → `configurationAccessEndpoint` DCRA 없음
**해결:** AKS에 `configurationAccessEndpoint` DCRA 생성 (위 3.3 참고)

---

## 5. 공통 진단 명령어

```bash
# OTel Operator 상태
kubectl get po -n opentelemetry-operator-system

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

## 6. 공통 체크리스트

| # | 항목 | 확인 방법 | 기대 결과 |
|---|------|----------|----------|
| 1 | OTel Operator 실행 | `kubectl get po -n opentelemetry-operator-system` | Running |
| 2 | ama-metrics 실행 | `kubectl get po -n kube-system -l rsName=ama-metrics` | Running |
| 3 | mdsd.err 에러 없음 | `kubectl exec ... cat mdsd.err` | 에러 없음 |
| 4 | DNS Private IP 해석 | `nslookup ...metrics.ingest.monitor.azure.com` | 10.2.0.x |
| 5 | DCRA 3개 존재 | REST API 조회 | configurationAccessEndpoint 포함 |

> 방식별 체크리스트: [Prometheus](monitoring-prometheus.md#7-정상-동작-체크리스트) | [Application Insights](monitoring-appinsights.md#7-정상-동작-체크리스트)
