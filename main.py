import json
import os
import requests
import math
from typing import List, Dict, Any
from fastapi import FastAPI
from pydantic import BaseModel, model_validator

try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    print("❌ OR-Tools 설치 필요")

HTTP_SESSION = requests.Session()
HTTP_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20))
HTTP_SESSION.mount("http://", requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20))
app = FastAPI()


@app.on_event("shutdown")
def close_http_session():
    HTTP_SESSION.close()

# ==========================================
# 1. 설정 및 환경변수
# ==========================================
DRIVER_START_TIME = 420  # 7:00 (제주물류센터 운영 시작 시간)
LOADING_TIME = 30        # 적재 시간 30분
WAREHOUSE_CLOSE_TIME = 1080  # 18:00 (오후 6:00, 물류센터 마감 시간 - 유조차 도착 마감, 이후 수송은 계속 가능)
GASOLINE_UNLOADING_TIME = 40  # 휘발유 하역 시간
DIESEL_UNLOADING_TIME = 30     # 등경유 하역 시간    

# [디버깅용] 현재 로드된 환경변수 키 목록 출력 (값은 보안상 출력 안함)
print("🔍 현재 서버 환경변수 목록:", list(os.environ.keys()))

# 환경변수 읽기 (유연한 처리)
NAVER_ID = os.environ.get("NAVER_CLIENT_ID") or os.environ.get("x-ncp-apigw-api-key-id")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET") or os.environ.get("x-ncp-apigw-api-key")

if not NAVER_ID or not NAVER_SECRET:
    print("⚠️ [경고] 네이버 지도 API 키가 설정되지 않았습니다.")
else:
    masked_id = NAVER_ID[:2] + "*" * 5 if NAVER_ID else "None"
    print(f"✅ 네이버 지도 API 키 로드 성공 (ID: {masked_id})")

# ==========================================
# 2. 데이터 모델
# ==========================================
class OrderItem(BaseModel):
    주유소명: str
    브랜드: str = ""  # "SK" 또는 "알뜰"
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    start_min: int = 420  # 7:00 (기본 방문 시작 시간)
    end_min: int = 1435  # 23:55 (기본 방문 종료 시간)
    priority: int = 2
    
    class Config:
        extra = 'allow'

    @model_validator(mode='before')
    @classmethod
    def flatten_data(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ['주문량', 'order', 'data']:
                if key in data and isinstance(data[key], dict):
                    inner = data[key]
                    if any(k in inner for k in ['휘발유', '경유', '등유']):
                        data.update(inner)
            for field in ['휘발유', '등유', '경유', 'start_min', 'end_min', 'priority']:
                if field in data:
                    try:
                        if data[field] == "" or data[field] is None:
                            data[field] = 0
                        else:
                            data[field] = int(data[field])
                    except:
                        data[field] = 0
            # 🔹 알뜰 주유소 휘발유 150드럼(150L 단위 기준) 이상 주문은 자동으로 우선순위 1로 설정
            try:
                brand = data.get('브랜드', '')
                gasoline = int(data.get('휘발유', 0) or 0)
                if brand == '알뜰' and gasoline >= 150:
                    # 사용자가 이미 priority=1을 명시했다면 그대로 유지, 그 외에는 1로 강제
                    data['priority'] = 1
            except Exception:
                # 변환 오류 시에는 우선순위 규칙만 건너뜀
                pass
        return data

class VehicleItem(BaseModel):
    차량번호: str
    유종: str
    수송용량: int

class OptimizationRequest(BaseModel):
    orders: List[OrderItem]
    vehicles: List[VehicleItem]

# ==========================================
# 3. 데이터 로드 (속도 최적화)
# ==========================================
NODE_INFO = {}
MATRIX_DATA = {} # 거리 데이터 캐시
DIST_CACHE = {}
PATH_CACHE = {}

def load_data():
    global NODE_INFO, MATRIX_DATA
    raw_data = None
    url = os.environ.get("JEJU_MATRIX_URL")
    
    # 1. URL 다운로드 시도
    if url:
        try:
            print(f"🌐 URL 데이터 다운로드 시도...")
            res = HTTP_SESSION.get(url, timeout=10)
            if res.status_code == 200: 
                raw_data = res.json()
                print("✅ URL에서 매트릭스 데이터 로드 성공!")
            else: 
                print(f"❌ URL 로드 실패: {res.status_code}")
        except Exception as e:
            print(f"❌ URL 에러: {e}")
    
    # 2. 파일 로드 (URL 실패 시 백업)
    if not raw_data and os.path.exists("jeju_distance_matrix_full.json"):
        try:
            with open("jeju_distance_matrix_full.json", "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            print("📂 로컬 파일에서 데이터 로드 성공!")
        except: pass

    if raw_data:
        # 리스트 구조 처리
        if isinstance(raw_data, list) and len(raw_data) > 0:
            raw_data = raw_data[0]
            
        # 좌표 정보 로드
        if "node_info" in raw_data:
            for node in raw_data["node_info"]:
                NODE_INFO[node["name"]] = {"lat": node["lat"], "lon": node["lon"]}
            print(f"✅ 좌표 데이터 준비 완료: {len(NODE_INFO)}개 지점")
            
        # 거리 매트릭스 로드 (핵심!)
        if "matrix" in raw_data:
            MATRIX_DATA = raw_data["matrix"]
            print(f"✅ 거리 매트릭스 준비 완료: {len(MATRIX_DATA)}개 지점")
        else:
            print("⚠️ [주의] JSON에 'matrix' 키가 없습니다. API 호출로 대체합니다(느림).")

load_data()

# 거리/시간 계산 (최적화된 버전)
def get_driving_time(start_name, end_name):
    key = f"{start_name}->{end_name}"
    if key in DIST_CACHE:
        return DIST_CACHE[key]

    if start_name == end_name:
        DIST_CACHE[key] = 0
        return 0

    # 1순위: 미리 로드된 매트릭스 파일 사용 (가장 빠름)
    if start_name in MATRIX_DATA and end_name in MATRIX_DATA[start_name]:
        try:
            # 데이터가 km 단위라고 가정하고 시간(분)으로 변환
            # 시속 40km/h 가정: 거리(km) * 1.5 = 소요시간(분)
            dist_val = float(MATRIX_DATA[start_name][end_name])
            minutes = max(5, int(dist_val * 1.5))
            # 너무 짧으면 기본 5분
            DIST_CACHE[key] = minutes
            return minutes
        except:
            pass

    # 2순위: 좌표가 없으면 기본값
    if start_name not in NODE_INFO or end_name not in NODE_INFO:
        DIST_CACHE[key] = 20
        return 20
    
    # 3순위: 네이버 API (매트릭스 파일에 데이터가 없을 때만 호출)
    # (최적화 단계에서 API를 남발하면 타임아웃 되므로 가급적 파일 사용 권장)
    if NAVER_ID and NAVER_SECRET:
        try:
            url = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
            headers = {
                "X-NCP-APIGW-API-KEY-ID": NAVER_ID,
                "X-NCP-APIGW-API-KEY": NAVER_SECRET
            }
            start = NODE_INFO[start_name]
            goal = NODE_INFO[end_name]
            params = {
                "start": f"{start['lon']},{start['lat']}",
                "goal": f"{goal['lon']},{goal['lat']}",
                "option": "trafast"
            }
            res = HTTP_SESSION.get(url, headers=headers, params=params, timeout=2)
            if res.status_code == 200:
                json_res = res.json()
                if json_res["code"] == 0:
                    minutes = int(json_res["route"]["trafast"][0]["summary"]["duration"] / 60000)
                    DIST_CACHE[key] = minutes
                    return minutes
        except: pass

    # 4순위: 하버사인 백업
    start = NODE_INFO[start_name]
    goal = NODE_INFO[end_name]
    R = 6371
    dLat = math.radians(goal['lat'] - start['lat'])
    dLon = math.radians(goal['lon'] - start['lon'])
    a = math.sin(dLat/2)**2 + math.cos(math.radians(start['lat'])) * math.cos(math.radians(goal['lat'])) * math.sin(dLon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    dist_km = R * c
    minutes = max(5, int((dist_km / 40) * 60 * 1.3))
    DIST_CACHE[key] = minutes
    return minutes

# 상세 경로 좌표 가져오기 (결과 생성 시에만 호출)
def get_detailed_path_geometry(start_name, end_name):
    key = f"{start_name}->{end_name}"
    if key in PATH_CACHE: return PATH_CACHE[key]
    if start_name not in NODE_INFO or end_name not in NODE_INFO: return []
    if not NAVER_ID or not NAVER_SECRET: return []

    try:
        start = NODE_INFO[start_name]
        goal = NODE_INFO[end_name]
        url = "https://maps.apigw.ntruss.com/map-direction/v1/driving"
        headers = {
            "X-NCP-APIGW-API-KEY-ID": NAVER_ID,
            "X-NCP-APIGW-API-KEY": NAVER_SECRET
        }
        params = {
            "start": f"{start['lon']},{start['lat']}",
            "goal": f"{goal['lon']},{goal['lat']}",
            "option": "trafast"
        }
        res = HTTP_SESSION.get(url, headers=headers, params=params, timeout=3)
        if res.status_code == 200:
            json_res = res.json()
            if json_res["code"] == 0:
                path_data = json_res["route"]["trafast"][0]["path"]
                PATH_CACHE[key] = path_data
                return path_data
    except: pass
    return []

# ==========================================
# 4. 배차 알고리즘
# ==========================================

def solve_multitrip_vrp(all_orders, all_vehicles, fuel_type):
    debug_logs = []
    pending_orders = []
    altteul_orders = []  # 🔹 (휘발유 전용) 알뜰 주유소 주문 리스트

    # 🔹 차량이 "다음 회차를 시작할 수 있는지" 판정 기준
    #   end_time = vehicle_state - LOADING_TIME 이므로,
    #   vehicle_state < WAREHOUSE_CLOSE_TIME + LOADING_TIME 가 되면 한 번 더 가능
    VEHICLE_AVAILABLE_THRESHOLD = WAREHOUSE_CLOSE_TIME + LOADING_TIME

    # 1단계: 주문 분리
    for o in all_orders:
        amt = o.휘발유 if fuel_type == "휘발유" else (o.등유 + o.경유)
        if amt > 0:
            # 휘발유 모드에서는 알뜰 주문을 우선적으로 7408 전용 리스트에 분리
            if fuel_type == "휘발유" and getattr(o, '브랜드', '') == '알뜰':
                altteul_orders.append(o)
            else:
                pending_orders.append(o)
        else:
            if fuel_type == "휘발유" and (o.등유 > 0 or o.경유 > 0):
                # 휘발유 모드에서 등/경유만 있는 주문은 디젤 단계에서 처리
                pass
            else:
                debug_logs.append(f"제외됨(주문량0): {o.주유소명}")

    my_vehicles = [v for v in all_vehicles if v.유종 == fuel_type]
    
    # 처리할 주문(알뜰 + 일반)이 하나도 없으면 스킵
    if (not pending_orders and not altteul_orders) or not my_vehicles:
        return {"status": "skipped", "routes": [], "debug_logs": debug_logs}

    # 휘발유인 경우 제주96바7408 차량 찾기 (알뜰 주유소 우선 차량)
    preferred_vehicle_idx = None
    if fuel_type == "휘발유":
        for i, v in enumerate(my_vehicles):
            if v.차량번호 == "제주96바7408":
                preferred_vehicle_idx = i
                break

    vehicle_state = {i: DRIVER_START_TIME for i in range(len(my_vehicles))} 
    vehicle_workload = {i: 0 for i in range(len(my_vehicles))}  # 누적 수송량
    final_schedule = []
    round_altteul = 1  # 🔹 7408 알뜰 전용 라운드 번호

    # ==========================================
    # 4-1. (휘발유) 알뜰 주유소: 7408이 가능할 때까지 먼저 최대한 배차
    #      - 여기서 처리하지 못한 알뜰은 나중에 SK와 합쳐서 7400/7403이 처리
    # ==========================================
    if fuel_type == "휘발유" and preferred_vehicle_idx is not None and altteul_orders:
        while altteul_orders and vehicle_state[preferred_vehicle_idx] < VEHICLE_AVAILABLE_THRESHOLD:
            preferred_vehicle = [my_vehicles[preferred_vehicle_idx]]
            preferred_start = [vehicle_state[preferred_vehicle_idx]]

            routes_preferred, remaining_altteul = run_ortools(
                altteul_orders, preferred_vehicle, preferred_start, fuel_type, preferred_vehicle_idx=0
            )

            if not routes_preferred:
                # 더 이상 7408로 배차 가능한 알뜰 주문이 없음
                break

            for r in routes_preferred:
                vehicle_state[preferred_vehicle_idx] = r['end_time'] + LOADING_TIME
                vehicle_workload[preferred_vehicle_idx] += r["total_load"]
                r['round'] = round_altteul
                r['vehicle_id'] = my_vehicles[preferred_vehicle_idx].차량번호
                final_schedule.append(r)

            altteul_orders = remaining_altteul
            round_altteul += 1
            if round_altteul > 10:
                debug_logs.append("알뜰 전용 단계에서 라운드 10회를 초과하여 안전 종료")
                break

        # 7408이 처리하지 못한 알뜰 주문은 SK 주문과 합쳐서 다음 단계에서 7400/7403이 함께 처리
        pending_orders = pending_orders + altteul_orders

    # ==========================================
    # 🔹 SK + (남은 알뜰) 단계 라운드 번호는 다시 1부터 시작
    # ==========================================
    round_main = 1

    # ==========================================
    # 4-2. 나머지 주문:
    #      - 휘발유: SK + (남은 알뜰) → 7400/7403 등으로 처리, 7408은 제외
    #      - 디젤: 전체 유효 주문을 모든 디젤 차량으로 처리
    # ==========================================
    while True:
        if not pending_orders:
            break
        available_indices = [i for i, t in vehicle_state.items() if t < VEHICLE_AVAILABLE_THRESHOLD]
        if not available_indices:
            break

        # 지금까지 누적 작업량이 적은 차량부터 우선 사용
        available_indices = [i for i, t in vehicle_state.items() if t < VEHICLE_AVAILABLE_THRESHOLD]
        if not available_indices:
            break
        available_indices.sort(key=lambda i: vehicle_workload[i])
        
        # 휘발유의 2단계(SK+남은 알뜰)는 7408을 제외하고 7400/7403 등만 사용
        if fuel_type == "휘발유" and preferred_vehicle_idx is not None:
            available_indices = [i for i in available_indices if i != preferred_vehicle_idx]
            if not available_indices:
                break
        
        current_vehicles = [my_vehicles[i] for i in available_indices]
        current_starts = [vehicle_state[i] for i in available_indices]
        
        # 남은 주문 처리 (휘발유: SK+남은 알뜰, 디젤: 전체)
        routes, remaining = run_ortools(pending_orders, current_vehicles, current_starts, fuel_type, preferred_vehicle_idx=None)
        
        # OR-Tools가 해를 찾지 못했을 때 처리
        if not routes and len(remaining) == len(pending_orders):
            # 모든 차량이 18:00 이후가 되었는지 확인
            all_vehicles_after_close = all(vehicle_state[i] >= VEHICLE_AVAILABLE_THRESHOLD for i in range(len(my_vehicles)))
            if all_vehicles_after_close:
                debug_logs.append(f"라운드 {round_main}: 모든 차량이 18:00 이후, 배차 종료")
                break
            
            # 일부 차량이 아직 18:00 전이면, 시간 제약이 너무 엄격한 주문을 필터링하고 재시도
            min_available_start = min(current_starts) if current_starts else WAREHOUSE_CLOSE_TIME
            processable_orders = []
            skipped_due_to_time = []
            depot = "제주물류센터"

            for order in pending_orders:
                travel_time = get_driving_time(depot, order.주유소명)
                service_time = GASOLINE_UNLOADING_TIME if fuel_type == "휘발유" else DIESEL_UNLOADING_TIME
                min_arrival = min_available_start + travel_time + service_time
                
                if order.end_min >= min_arrival:
                    processable_orders.append(order)
                else:
                    skipped_due_to_time.append(order.주유소명)
            
            if skipped_due_to_time:
                debug_logs.append(
                    f"라운드 {round_main}: 시간 제약으로 처리 불가능한 주문 {len(skipped_due_to_time)}개: "
                    f"{', '.join(skipped_due_to_time[:3])}{'...' if len(skipped_due_to_time) > 3 else ''}"
                )
            
            if processable_orders:
                pending_orders = processable_orders
                debug_logs.append(
                    f"라운드 {round_main}: OR-Tools 해 탐색 실패, 처리 가능한 주문 {len(processable_orders)}개로 재시도"
                )
                continue  # 다음 라운드로
            else:
                debug_logs.append(f"라운드 {round_main}: 처리 가능한 주문 없음, 배차 종료")
                break

        # 해를 찾았을 때 차량 상태 업데이트
        for r in routes:
            real_v_idx = available_indices[r['internal_idx']]
            vehicle_state[real_v_idx] = r['end_time'] + LOADING_TIME
            vehicle_workload[real_v_idx] += r["total_load"]
            r['round'] = round_main
            r['vehicle_id'] = my_vehicles[real_v_idx].차량번호
            final_schedule.append(r)
            
        pending_orders = remaining
        round_main += 1
        
        # 안전장치: 라운드 과도 증가 방지
        if round_main > 10:
            debug_logs.append("일반 단계에서 라운드 10회를 초과하여 안전 종료")
            break

    # 미처리 주문 상세 정보 생성
    skipped_list = []
    for o in pending_orders:
        order_info = {
            "주유소명": o.주유소명,
            "브랜드": getattr(o, '브랜드', ''),
            "요청물량": {
                "휘발유": o.휘발유 if fuel_type == "휘발유" else 0,
                "등유": o.등유 if fuel_type != "휘발유" else 0,
                "경유": o.경유 if fuel_type != "휘발유" else 0
            },
            "총요청물량": o.휘발유 if fuel_type == "휘발유" else (o.등유 + o.경유),
            "시간제약": {
                "시작시간": f"{o.start_min // 60:02d}:{o.start_min % 60:02d}",
                "종료시간": f"{o.end_min // 60:02d}:{o.end_min % 60:02d}",
                "start_min": o.start_min,
                "end_min": o.end_min
            },
            "우선순위": o.priority,
            "미처리이유": "시간/차량 부족"
        }
        skipped_list.append(order_info)

    return {
        "status": "success", 
        "total_delivered": sum(r['total_load'] for r in final_schedule),
        "total_vehicles_used": len(set(r['vehicle_id'] for r in final_schedule)),
        "routes": final_schedule, 
        "unassigned_orders": skipped_list,
        "unassigned_count": len(skipped_list),
        "unassigned_total_load": sum(o["총요청물량"] for o in skipped_list),
        "debug_logs": debug_logs
    }

def run_ortools(orders, vehicles, start_times, fuel_type, preferred_vehicle_idx=None):
    """
    preferred_vehicle_idx: 알뜰 주유소를 처리할 우선 차량 인덱스 (None이면 제약 없음)
    """
    depot = "제주물류센터"
    locs = [depot] + [o.주유소명 for o in orders]
    N = len(locs)
    
    # 1. 거리 매트릭스 생성 (여기서 API 대신 로컬 매트릭스 활용)
    durations = [[0]*N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i != j: durations[i][j] = get_driving_time(locs[i], locs[j])

    manager = pywrapcp.RoutingIndexManager(N, len(vehicles), 0)
    routing = pywrapcp.RoutingModel(manager)
    
    # 🔹 알뜰 주유소는 preferred_vehicle_idx 차량만 방문하도록 제약
    # 참고: solve_multitrip_vrp에서 이미 preferred_vehicle 리스트에 제주96바7408만 넣었으므로
    # vehicles 리스트에 원하는 차량만 들어있어 자동으로 제약이 적용됩니다.
    # OR-Tools 9.12 이상에서는 SetAllowedVehiclesForIndex 메서드를 사용할 수 있지만,
    # 현재 구조에서는 vehicles 리스트 필터링만으로도 충분합니다.
    # 
    # 만약 추가 제약이 필요한 경우 (예: 여러 차량 중 특정 차량만 선택):
    # if preferred_vehicle_idx is not None and fuel_type == "휘발유":
    #     for i, order in enumerate(orders):
    #         if getattr(order, '브랜드', '') == '알뜰':
    #             index = manager.NodeToIndex(i + 1)
    #             # OR-Tools 9.12+ 에서는 SetAllowedVehiclesForIndex 사용 가능
    #             routing.SetAllowedVehiclesForIndex([preferred_vehicle_idx], index)

    def time_callback(from_i, to_i):
        f, t = manager.IndexToNode(from_i), manager.IndexToNode(to_i)
        if t != 0:
            # 유종에 따라 하역 시간 다르게 적용
            service = GASOLINE_UNLOADING_TIME if fuel_type == "휘발유" else DIESEL_UNLOADING_TIME
        else:
            service = 0
        return durations[f][t] + service

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    routing.AddDimension(transit_idx, 1440, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    
    if hasattr(time_dim, "SetSlackCostCoefficientForAllVehicles"):
        time_dim.SetSlackCostCoefficientForAllVehicles(1)
    else:
        print("⚠️ SetSlackCostCoefficientForAllVehicles 지원 안 하는 OR-Tools 버전입니다.")

    
    for i in range(len(vehicles)):
        idx = routing.Start(i)
        start_time = int(start_times[i])
        # 🔹 모든 차량이 정확히 지정된 시간(7:00)에 시작하도록 제약 설정
        time_dim.CumulVar(idx).SetMin(start_time)
        time_dim.CumulVar(idx).SetMax(start_time)  # 최소값과 최대값을 동일하게 설정하여 정확히 해당 시간에 시작
        # 참고: 차량이 물류센터에 돌아오는 시간은 제약하지 않음 (18:00 이후에도 수송 가능)
        # 새로운 배차 시작은 WAREHOUSE_CLOSE_TIME(18:00) 조건으로 제어됨
    
    for i, order in enumerate(orders):
        index = manager.NodeToIndex(i + 1)
        # 🔹 주문의 종료 시간이 너무 이른 경우를 대비하여, 최소한 가장 이른 차량의 시작 시간 + 이동시간 + 하역시간 이상으로 설정
        # (단, 원래 end_min이 더 늦으면 원래 값 사용, 또는 18:00 이후인 경우 23:59까지 허용)
        min_start_time = min(start_times) if start_times else DRIVER_START_TIME
        min_travel_time = get_driving_time(depot, order.주유소명)
        min_service_time = GASOLINE_UNLOADING_TIME if fuel_type == "휘발유" else DIESEL_UNLOADING_TIME
        min_arrival_time = min_start_time + min_travel_time + min_service_time
        
        # 원래 end_min이 18:00 이후이거나, 계산된 최소 도착 시간보다 늦으면 그대로 사용
        # 원래 end_min이 너무 이르면 최소 도착 시간으로 조정 (단, 원래 값이 18:00 이후면 원래 값 사용)
        if order.end_min >= WAREHOUSE_CLOSE_TIME:
            effective_end_min = order.end_min  # 18:00 이후면 원래 값 사용
        else:
            effective_end_min = max(order.end_min, min_arrival_time)  # 더 이른 값 중 큰 값 사용
        
        time_dim.CumulVar(index).SetRange(order.start_min, effective_end_min)
        
        # 🔹 priority에 따라 패널티를 다르게 주되, 모든 주문은 Disjunction으로 "선택적 방문"으로 모델링
        #    - priority 1: 거의 반드시 가야 하지만, 물리적으로 불가능한 경우를 위해서라도 드물게 제외 가능하게 함
        #    - priority 2: 기본값 (일반 주문)
        #    - priority 3 이상: 상대적으로 덜 중요한 주문
        if order.priority <= 1:
            penalty = 10_000_000  # 매우 크게 (가능하면 반드시 방문)
        elif order.priority == 2:
            penalty = 1_000_000   # 기본 패널티
        else:
            penalty = 100_000     # 덜 중요한 주문은 상대적으로 낮게
        routing.AddDisjunction([index], penalty)


    demands = [0] + [ (o.휘발유 if fuel_type=="휘발유" else o.등유+o.경유) for o in orders ]
    def demand_callback(from_i):
        return demands[manager.IndexToNode(from_i)]
    cap_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(cap_idx, 0, [v.수송용량 for v in vehicles], True, "Capacity")

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.time_limit.seconds = 10  # 🔹 최적화 시간을 늘려서 더 나은 해를 찾도록
    # 🔹 차량이 가능한 한 빨리 시작하도록 최적화
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    solution = routing.SolveWithParameters(search_params)
    
    routes = []
    fulfilled_indices = set()
    
    if solution:
        for v_idx in range(len(vehicles)):
            index = routing.Start(v_idx)
            path = []
            load = 0
            # geometry_list = [] # 상세 경로 (일단 비활성화)

            while not routing.IsEnd(index):
                node_idx = manager.IndexToNode(index)
                if node_idx > 0: fulfilled_indices.add(node_idx - 1)
                
                t_val = solution.Min(time_dim.CumulVar(index))
                node_name = locs[node_idx]
                coord = NODE_INFO.get(node_name, {"lat": 0, "lon": 0})
                
                path.append({
                    "location": node_name,
                    "lat": coord["lat"], "lon": coord["lon"],
                    "time": t_val, "load": demands[node_idx]
                })
                load += demands[node_idx]

                next_index = solution.Value(routing.NextVar(index))
                # 상세 경로는 여기서 API 호출 (현재 비활성화 - 응답 데이터 축소)
                # if not routing.IsEnd(next_index):
                #     next_node_idx = manager.IndexToNode(next_index)
                #     segment_path = get_detailed_path_geometry(node_name, locs[next_node_idx])
                #     if segment_path: geometry_list.extend(segment_path)
                
                index = next_index

            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            depot_coord = NODE_INFO.get(depot, {"lat": 0, "lon": 0})
            
            # 마지막 지점 → 물류센터 상세 경로도 비활성화
            # last_loc = path[-1]["location"]
            # return_path = get_detailed_path_geometry(last_loc, depot)
            # if return_path: geometry_list.extend(return_path)
            
            path.append({
                "location": depot,
                "lat": depot_coord["lat"], "lon": depot_coord["lon"],
                "time": end_time, "load": 0
            })
            
            if len(path) > 2:
                # 시작 시간 계산 (첫 번째 노드의 시간)
                start_time = solution.Min(time_dim.CumulVar(routing.Start(v_idx))) if len(path) > 0 else 0
                routes.append({
                    "internal_idx": v_idx, 
                    "start_time": start_time,
                    "start_time_formatted": f"{start_time // 60:02d}:{start_time % 60:02d}",
                    "end_time": end_time,
                    "end_time_formatted": f"{end_time // 60:02d}:{end_time % 60:02d}",
                    "total_load": load, 
                    "path": path
                    # "geometry": geometry_list  # 상세 경로 응답 비활성화
                })
                
    remaining = [orders[i] for i in range(len(orders)) if i not in fulfilled_indices]
    return routes, remaining

@app.post("/optimize")
def optimize(req: OptimizationRequest):
    gas = solve_multitrip_vrp(req.orders, req.vehicles, "휘발유")
    diesel = solve_multitrip_vrp(req.orders, req.vehicles, "등경유")
    return {"gasoline": gas, "diesel": diesel}

@app.get("/")
def health():
    return {
        "status": "ok", 
        "matrix_loaded": len(MATRIX_DATA) > 0, 
        "naver_api": bool(NAVER_ID)
    }
