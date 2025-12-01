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

app = FastAPI()

# ==========================================
# 1. 설정 및 환경변수
# ==========================================
DRIVER_START_TIME = 360 
LOADING_TIME = 20      
UNLOADING_TIME = 30    

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
    휘발유: int = 0
    등유: int = 0
    경유: int = 0
    start_min: int = 540
    end_min: int = 1080
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
            res = requests.get(url, timeout=15)
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
    if key in DIST_CACHE: return DIST_CACHE[key]
    
    # 1순위: 미리 로드된 매트릭스 파일 사용 (가장 빠름)
    if start_name in MATRIX_DATA and end_name in MATRIX_DATA[start_name]:
        try:
            # 데이터가 km 단위라고 가정하고 시간(분)으로 변환
            # 시속 40km/h 가정: 거리(km) * 1.5 = 소요시간(분)
            dist_val = float(MATRIX_DATA[start_name][end_name])
            minutes = int(dist_val * 1.5)
            # 너무 짧으면 기본 5분
            return max(5, minutes)
        except:
            pass

    # 2순위: 좌표가 없으면 기본값
    if start_name not in NODE_INFO or end_name not in NODE_INFO: 
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
            res = requests.get(url, headers=headers, params=params, timeout=3)
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
    return max(5, int((dist_km / 40) * 60 * 1.3))

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
        res = requests.get(url, headers=headers, params=params, timeout=5)
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
    for o in all_orders:
        amt = o.휘발유 if fuel_type == "휘발유" else (o.등유 + o.경유)
        if amt > 0: pending_orders.append(o)
        else:
            if fuel_type == "휘발유" and (o.등유 > 0 or o.경유 > 0): pass
            else: debug_logs.append(f"제외됨(주문량0): {o.주유소명}")

    my_vehicles = [v for v in all_vehicles if v.유종 == fuel_type]
    
    if not pending_orders or not my_vehicles:
        return {"status": "skipped", "routes": [], "debug_logs": debug_logs}

    vehicle_state = {i: DRIVER_START_TIME for i in range(len(my_vehicles))} 
    final_schedule = []
    
    for round_num in range(1, 6):
        if not pending_orders: break
        available_indices = [i for i, t in vehicle_state.items() if t < 1080 - 60]
        if not available_indices: break
        
        current_vehicles = [my_vehicles[i] for i in available_indices]
        current_starts = [vehicle_state[i] for i in available_indices]
        
        routes, remaining = run_ortools(pending_orders, current_vehicles, current_starts, fuel_type)
        
        if not routes and len(remaining) == len(pending_orders):
            break

        for r in routes:
            real_v_idx = available_indices[r['internal_idx']]
            vehicle_state[real_v_idx] = r['end_time'] + LOADING_TIME
            r['round'] = round_num
            r['vehicle_id'] = my_vehicles[real_v_idx].차량번호
            final_schedule.append(r)
            
        pending_orders = remaining

    skipped_list = [{"name": o.주유소명, "reason": "시간/차량 부족"} for o in pending_orders]

    return {
        "status": "success", 
        "total_delivered": sum(r['total_load'] for r in final_schedule),
        "routes": final_schedule, 
        "unassigned_orders": skipped_list,
        "debug_logs": debug_logs
    }

def run_ortools(orders, vehicles, start_times, fuel_type):
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

    def time_callback(from_i, to_i):
        f, t = manager.IndexToNode(from_i), manager.IndexToNode(to_i)
        service = UNLOADING_TIME if t != 0 else 0
        return durations[f][t] + service

    transit_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)
    routing.AddDimension(transit_idx, 1440, 1440, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    
    # ✨ 추가된 줄: 전체 경로 길이를 줄이도록 유도해 차량 간 불균형 완화
    time_dim.SetGlobalSpanCostCoefficient(100)

    for i in range(len(vehicles)):
        idx = routing.Start(i)
        time_dim.CumulVar(idx).SetMin(int(start_times[i]))

    time_dim.CumulVar(routing.Start(0)).SetRange(0, 1440)
    for i, order in enumerate(orders):
        index = manager.NodeToIndex(i + 1)
        time_dim.CumulVar(index).SetRange(order.start_min, order.end_min)
        penalty = 100000 if order.priority == 1 else 1000
        routing.AddDisjunction([index], penalty)

    demands = [0] + [ (o.휘발유 if fuel_type=="휘발유" else o.등유+o.경유) for o in orders ]
    def demand_callback(from_i):
        return demands[manager.IndexToNode(from_i)]
    cap_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(cap_idx, 0, [v.수송용량 for v in vehicles], True, "Capacity")

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.time_limit.seconds = 5
    solution = routing.SolveWithParameters(search_params)
    
    routes = []
    fulfilled_indices = set()
    
    if solution:
        for v_idx in range(len(vehicles)):
            index = routing.Start(v_idx)
            path = []
            load = 0
            geometry_list = [] # 상세 경로

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

                # 상세 경로는 여기서 API 호출 (횟수 적음)
                next_index = solution.Value(routing.NextVar(index))
                if not routing.IsEnd(next_index):
                    next_node_idx = manager.IndexToNode(next_index)
                    segment_path = get_detailed_path_geometry(node_name, locs[next_node_idx])
                    if segment_path: geometry_list.extend(segment_path)
                
                index = next_index

            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            depot_coord = NODE_INFO.get(depot, {"lat": 0, "lon": 0})
            
            last_loc = path[-1]["location"]
            return_path = get_detailed_path_geometry(last_loc, depot)
            if return_path: geometry_list.extend(return_path)
            
            path.append({
                "location": depot,
                "lat": depot_coord["lat"], "lon": depot_coord["lon"],
                "time": end_time, "load": 0
            })
            
            if len(path) > 2:
                routes.append({
                    "internal_idx": v_idx, 
                    "end_time": end_time, 
                    "total_load": load, 
                    "path": path,
                    "geometry": geometry_list
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
