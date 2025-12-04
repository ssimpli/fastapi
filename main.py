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
    
    # 🔹 디버깅: 입력 차량 데이터 확인
    debug_logs.append(f"입력 차량 수: {len(all_vehicles)}, {fuel_type} 차량 수: {len(my_vehicles)}")
    for v in all_vehicles:
        if v.차량번호 in ["제주96바7400", "제주96바7403"]:
            debug_logs.append(f"입력데이터: {v.차량번호} - 유종: '{v.유종}', 수송용량: {v.수송용량}, 필터링 후 포함: {v in my_vehicles}")
    
    if not pending_orders or not my_vehicles:
        return {"status": "skipped", "routes": [], "debug_logs": debug_logs}
    
    # 🔹 주문을 거리순으로 정렬 (가까운 곳부터 배차하여 빠르게 복귀 가능하도록)
    # 우선순위: 1) priority=1인 긴급 주문, 2) 물류센터까지의 거리 (가까운 순)
    depot = "제주물류센터"
    def get_order_priority(order):
        # priority가 1이면 최우선 (거리 무관)
        if order.priority == 1:
            return (0, 0)  # 최우선
        # 그 외에는 물류센터까지의 거리로 정렬
        distance = get_driving_time(depot, order.주유소명)
        return (1, distance)  # priority=1이 아닌 주문은 거리순
    
    pending_orders.sort(key=get_order_priority)

    # 🔹 휘발유인 경우 제주96바7408 차량 찾기 (알뜰 주유소 전용)
    preferred_vehicle_idx = None
    if fuel_type == "휘발유":
        for i, v in enumerate(my_vehicles):
            if v.차량번호 == "제주96바7408":
                preferred_vehicle_idx = i
                break

    # 🔹 vehicle_state: 차량이 물류센터에 도착한 시간 (적재 시작 가능 시간)
    # 첫 배차: 7:00에 물류센터에 도착 → 7:00~7:30 적재 → 7:30 출발
    # 이후 배차: 물류센터 도착 시간 → 적재 → 출발
    vehicle_state = {i: DRIVER_START_TIME for i in range(len(my_vehicles))} 
    vehicle_workload = {i: 0 for i in range(len(my_vehicles))}  # 🔹추가: 누적 수송량
    final_schedule = []
    
    # 🔹 디버깅: 초기 차량 목록 확인
    for i, v in enumerate(my_vehicles):
        if v.차량번호 in ["제주96바7400", "제주96바7403"]:
            debug_logs.append(f"초기상태: {v.차량번호} - 유종: {v.유종}, 수송용량: {v.수송용량}, 인덱스: {i}")
    
    for round_num in range(1, 6):
        if not pending_orders: break
        # 🔹 차량이 18:00 이전에 물류센터에 도착했는지 확인
        # 18:00 이전에 도착하면 적재를 시작할 수 있음 (적재는 18:00 이후에도 가능)
        available_indices = [i for i, arrival_time in vehicle_state.items() if arrival_time < WAREHOUSE_CLOSE_TIME]
        if not available_indices: break

        # 🔹 휘발유이고 알뜰 주유소 주문이 있는 경우, 제주96바7408 우선 사용
        if fuel_type == "휘발유" and preferred_vehicle_idx is not None and preferred_vehicle_idx in available_indices:
            # 알뜰 주유소 주문 분리
            altteul_orders = [o for o in pending_orders if getattr(o, '브랜드', '') == '알뜰']
            sk_orders = [o for o in pending_orders if getattr(o, '브랜드', '') != '알뜰']
            
            # 🔹 알뜰 주유소 주문도 거리순으로 정렬
            if altteul_orders:
                altteul_orders.sort(key=get_order_priority)
            
            if altteul_orders:
                # 1단계: 알뜰 주유소 주문에 대해 제주96바7408만 사용
                preferred_vehicle = [my_vehicles[preferred_vehicle_idx]]
                # 🔹 차량이 물류센터에 도착한 후 적재를 완료한 시간이 출발 시간
                preferred_start = [vehicle_state[preferred_vehicle_idx] + LOADING_TIME]
                
                # 🔹 디버깅: 알뜰 주유소 전용 run_ortools 호출
                debug_logs.append(f"라운드 {round_num}: 알뜰 주유소 전용 run_ortools 호출 - 차량: 제주96바7408, 주문수: {len(altteul_orders)}")
                routes_preferred, remaining_altteul = run_ortools(
                    altteul_orders, preferred_vehicle, preferred_start, fuel_type, preferred_vehicle_idx=0
                )
                
                # 제주96바7408로 처리된 경우 상태 업데이트
                if routes_preferred:
                    for r in routes_preferred:
                        # 🔹 차량이 물류센터에 도착한 시간으로 저장 (적재 시작 가능 시간)
                        vehicle_state[preferred_vehicle_idx] = r['end_time']
                        vehicle_workload[preferred_vehicle_idx] += r["total_load"]
                        r['round'] = round_num
                        r['vehicle_id'] = my_vehicles[preferred_vehicle_idx].차량번호
                        final_schedule.append(r)
                    
                    # 남은 알뜰 주문과 SK 주문을 합쳐서 모든 차량으로 처리
                    remaining_orders = remaining_altteul + sk_orders
                else:
                    # 제주96바7408로 처리 못한 경우, 모든 알뜰 주문과 SK 주문을 합쳐서 처리
                    remaining_orders = altteul_orders + sk_orders
            else:
                # 알뜰 주유소 주문이 없으면 기존 로직대로
                remaining_orders = pending_orders
        else:
            # 등경유이거나 제주96바7408이 사용 불가능한 경우 기존 로직
            remaining_orders = pending_orders

        # 🔹 각 라운드에서도 주문을 거리순으로 정렬 (가까운 곳부터 배차)
        remaining_orders.sort(key=get_order_priority)

        # 🔹 지금까지 누적 작업량이 적은 차량부터 우선 사용
        available_indices = [i for i, t in vehicle_state.items() if t < WAREHOUSE_CLOSE_TIME]
        if not available_indices: break
        
        # 🔹 디버깅: available_indices 계산 후 7400, 7403 상태 확인
        for i in available_indices:
            v = my_vehicles[i]
            if v.차량번호 in ["제주96바7400", "제주96바7403"]:
                debug_logs.append(f"라운드 {round_num}: {v.차량번호} - available_indices에 포함됨 (인덱스: {i}, 도착시간: {vehicle_state[i]}분, 작업량: {vehicle_workload[i]})")
        
        available_indices.sort(key=lambda i: vehicle_workload[i])
        
        # 🔹 휘발유이고 SK 주유소 주문이 포함된 경우, 제주96바7408 제외
        if fuel_type == "휘발유" and preferred_vehicle_idx is not None:
            # remaining_orders에 SK 주유소 주문이 있는지 확인
            has_sk_orders = any(getattr(o, '브랜드', '') != '알뜰' for o in remaining_orders)
            if has_sk_orders:
                # SK 주유소 주문이 있으면 제주96바7408 제외
                available_indices = [i for i in available_indices if i != preferred_vehicle_idx]
                if not available_indices: break
        
        current_vehicles = [my_vehicles[i] for i in available_indices]
        
        # 🔹 디버깅: current_vehicles에 7400, 7403 포함 여부 확인
        current_vehicle_numbers = [v.차량번호 for v in current_vehicles]
        for target in ["제주96바7400", "제주96바7403"]:
            if target in current_vehicle_numbers:
                debug_logs.append(f"라운드 {round_num}: {target} - current_vehicles에 포함됨 (총 {len(current_vehicles)}대)")
            else:
                debug_logs.append(f"라운드 {round_num}: {target} - current_vehicles에 포함되지 않음 (현재 차량: {', '.join(current_vehicle_numbers)})")
        # 🔹 차량이 물류센터에 도착한 후 적재를 완료한 시간이 출발 시간
        current_starts = [vehicle_state[i] + LOADING_TIME for i in available_indices]
        
        # 🔹 남은 주문 처리 시에는 제약 없이 모든 차량 사용 (단, 제주96바7408은 SK 주유소에 배차 안됨)
        # 🔹 디버깅: run_ortools 호출 전 상태
        debug_logs.append(f"라운드 {round_num}: run_ortools 호출 - 차량수: {len(current_vehicles)}, 주문수: {len(remaining_orders)}, 차량목록: {[v.차량번호 for v in current_vehicles]}")
        routes, remaining = run_ortools(remaining_orders, current_vehicles, current_starts, fuel_type, preferred_vehicle_idx=None)
        
        if not routes and len(remaining) == len(remaining_orders):
            break

        # 🔹 사용된 차량 인덱스 추적
        used_vehicle_indices = set()
        used_vehicle_numbers = []
        for r in routes:
            real_v_idx = available_indices[r['internal_idx']]
            used_vehicle_indices.add(real_v_idx)
            vehicle_number = my_vehicles[real_v_idx].차량번호
            used_vehicle_numbers.append(vehicle_number)
            
            # 🔹 차량이 물류센터에 도착한 시간으로 저장 (적재 시작 가능 시간)
            vehicle_state[real_v_idx] = r['end_time']
            vehicle_workload[real_v_idx] += r["total_load"]       # 🔹이 차량 누적 수송량 증가
            
            r['round'] = round_num
            r['vehicle_id'] = vehicle_number
            final_schedule.append(r)
        
        # 🔹 디버깅: 실제로 배차된 차량 확인
        if routes:
            debug_logs.append(f"라운드 {round_num}: 배차 완료 - 사용된 차량: {', '.join(used_vehicle_numbers)}, 배차수: {len(routes)}")
        else:
            debug_logs.append(f"라운드 {round_num}: 배차 실패 - routes가 비어있음")
        
        # 🔹 디버깅: 사용되지 않은 차량 확인
        unused_indices = [i for i in available_indices if i not in used_vehicle_indices]
        if unused_indices:
            unused_vehicles = [my_vehicles[i].차량번호 for i in unused_indices]
            debug_logs.append(f"라운드 {round_num}: 사용되지 않은 차량: {', '.join(unused_vehicles)}")
        
        # 🔹 디버깅: 7400, 7403 차량 상태 추적
        for i, v in enumerate(my_vehicles):
            if v.차량번호 in ["제주96바7400", "제주96바7403"]:
                is_available = i in available_indices
                arrival_time = vehicle_state[i]
                workload = vehicle_workload[i]
                debug_logs.append(f"라운드 {round_num}: {v.차량번호} - 사용가능: {is_available}, 도착시간: {arrival_time}분({arrival_time//60:02d}:{arrival_time%60:02d}), 작업량: {workload}, 인덱스: {i}")
            
        pending_orders = remaining

    # 🔹 미처리 주문 상세 정보 생성
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
    # 🔹 디버깅: run_ortools에 전달된 차량 목록 확인
    debug_info = []
    for i, v in enumerate(vehicles):
        if v.차량번호 in ["제주96바7400", "제주96바7403"]:
            debug_info.append(f"run_ortools: {v.차량번호} - vehicles 리스트에 포함됨 (인덱스: {i}, 수송용량: {v.수송용량}, 시작시간: {start_times[i]}분)")
    
    # 🔹 디버깅: 주문 정보 요약
    if orders:
        total_demand = sum(o.휘발유 if fuel_type=="휘발유" else o.등유+o.경유 for o in orders)
        total_capacity = sum(v.수송용량 for v in vehicles)
        # 주문 시간 제약 확인
        time_ranges = [(o.start_min, o.end_min) for o in orders]
        min_start = min(o.start_min for o in orders) if orders else 0
        max_end = max(o.end_min for o in orders) if orders else 0
        debug_info.append(f"run_ortools: 주문 요약 - 주문수: {len(orders)}, 총요청량: {total_demand}, 총수송용량: {total_capacity}, 차량수: {len(vehicles)}")
        debug_info.append(f"run_ortools: 시간제약 - 최소시작: {min_start}분({min_start//60:02d}:{min_start%60:02d}), 최대종료: {max_end}분({max_end//60:02d}:{max_end%60:02d}), 차량시작시간: {[f'{s//60:02d}:{s%60:02d}' for s in start_times]}")
    
    depot = "제주물류센터"
    locs = [depot] + [o.주유소명 for o in orders]
    N = len(locs)
    
    # 1. 거리 매트릭스 생성 (여기서 API 대신 로컬 매트릭스 활용)
    durations = [[0]*N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            if i != j: durations[i][j] = get_driving_time(locs[i], locs[j])
    
    # 🔹 디버깅: 물류센터에서 첫 번째 주문까지의 이동 시간 확인
    if orders and len(orders) > 0:
        first_order_name = orders[0].주유소명
        depot_to_first = get_driving_time("제주물류센터", first_order_name)
        debug_info.append(f"run_ortools: 물류센터 → {first_order_name} 이동시간: {depot_to_first}분")

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
        # 🔹 모든 차량이 정확히 지정된 시간에 시작하도록 제약 설정
        time_dim.CumulVar(idx).SetMin(start_time)
        time_dim.CumulVar(idx).SetMax(start_time)  # 최소값과 최대값을 동일하게 설정하여 정확히 해당 시간에 시작
        # 참고: 차량이 물류센터에 돌아오는 시간은 제약하지 않음 (18:00 이후에도 수송 가능)
        # 새로운 배차 시작은 WAREHOUSE_CLOSE_TIME(18:00) 조건으로 제어됨
        
        # 🔹 디버깅: 차량 시작 시간 확인
        if vehicles[i].차량번호 in ["제주96바7400", "제주96바7403"]:
            debug_info.append(f"run_ortools: {vehicles[i].차량번호} - 시작시간 제약: {start_time}분({start_time//60:02d}:{start_time%60:02d})")
    
    for i, order in enumerate(orders):
        index = manager.NodeToIndex(i + 1)
        time_dim.CumulVar(index).SetRange(order.start_min, order.end_min)
        
        # 🔹 디버깅: 주문 시간 제약 확인 (처음 3개만)
        if i < 3:
            debug_info.append(f"run_ortools: 주문[{i}] {order.주유소명} - 시간제약: {order.start_min}분({order.start_min//60:02d}:{order.start_min%60:02d}) ~ {order.end_min}분({order.end_min//60:02d}:{order.end_min%60:02d}), 요청량: {order.휘발유 if fuel_type=='휘발유' else order.등유+order.경유}")
    
        if order.priority == 1:
            # 🔹 필수 방문: Disjunction 안 걸어줌
            # (솔버가 이 노드를 빼버릴 수 없음)
            pass
        else:
            # 🔹 상대적으로 덜 중요한 주문만 선택적으로 방문
            penalty = 1_000_000  # 꽤 크게
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
    
    # 🔹 디버깅: OR-Tools solution 상태 확인
    if solution is None:
        debug_info.append(f"run_ortools: ⚠️ OR-Tools가 solution을 찾지 못함 (차량수: {len(vehicles)}, 주문수: {len(orders)})")
        # 🔹 solution을 찾지 못한 이유 분석
        # 시간 제약이 너무 엄격한 주문 확인
        tight_orders = []
        for o in orders:
            time_window = o.end_min - o.start_min
            if time_window < 120:  # 2시간 미만
                tight_orders.append(f"{o.주유소명}({time_window}분)")
        if tight_orders:
            debug_info.append(f"run_ortools: ⚠️ 시간제약이 엄격한 주문: {', '.join(tight_orders[:5])}")
        
        # 🔹 첫 번째 주문의 도착 가능 시간 계산
        if orders and len(orders) > 0:
            first_order = orders[0]
            min_start_time = min(start_times) if start_times else 450
            depot_to_first = get_driving_time("제주물류센터", first_order.주유소명)
            earliest_arrival = min_start_time + depot_to_first
            debug_info.append(f"run_ortools: 첫 주문({first_order.주유소명}) 도착 가능 시간: {earliest_arrival}분({earliest_arrival//60:02d}:{earliest_arrival%60:02d}), 요구시간: {first_order.start_min}분({first_order.start_min//60:02d}:{first_order.start_min%60:02d}) ~ {first_order.end_min}분({first_order.end_min//60:02d}:{first_order.end_min%60:02d})")
            if earliest_arrival > first_order.end_min:
                debug_info.append(f"run_ortools: ⚠️ 첫 주문 도착 불가능! (도착시간 {earliest_arrival}분 > 종료시간 {first_order.end_min}분)")
        
        for v in vehicles:
            if v.차량번호 in ["제주96바7400", "제주96바7403"]:
                debug_info.append(f"run_ortools: {v.차량번호} - solution이 None이어서 확인 불가")
    else:
        debug_info.append(f"run_ortools: ✅ OR-Tools가 solution을 찾음 (차량수: {len(vehicles)}, 주문수: {len(orders)})")
    
    routes = []
    fulfilled_indices = set()
    
    if solution:
        # 🔹 디버깅: OR-Tools에서 선택된 차량 확인
        used_vehicle_indices_in_ortools = set()
        for v_idx in range(len(vehicles)):
            index = routing.Start(v_idx)
            path = []
            load = 0
            # geometry_list = [] # 상세 경로 (디버깅 중이므로 비활성화)

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

                # 상세 경로는 여기서 API 호출 (횟수 적음) - 디버깅 중이므로 비활성화
                next_index = solution.Value(routing.NextVar(index))
                # if not routing.IsEnd(next_index):
                #     next_node_idx = manager.IndexToNode(next_index)
                #     segment_path = get_detailed_path_geometry(node_name, locs[next_node_idx])
                #     if segment_path: geometry_list.extend(segment_path)
                
                index = next_index

            node_idx = manager.IndexToNode(index)
            end_time = solution.Min(time_dim.CumulVar(index))
            depot_coord = NODE_INFO.get(depot, {"lat": 0, "lon": 0})
            
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
                used_vehicle_indices_in_ortools.add(v_idx)
                routes.append({
                    "internal_idx": v_idx, 
                    "start_time": start_time,
                    "start_time_formatted": f"{start_time // 60:02d}:{start_time % 60:02d}",
                    "end_time": end_time,
                    "end_time_formatted": f"{end_time // 60:02d}:{end_time % 60:02d}",
                    "total_load": load, 
                    "path": path,
                    # "geometry": geometry_list  # 디버깅 중이므로 비활성화
                })
        
        # 🔹 디버깅: OR-Tools에서 사용되지 않은 차량 확인
        for v_idx, v in enumerate(vehicles):
            if v.차량번호 in ["제주96바7400", "제주96바7403"]:
                if v_idx in used_vehicle_indices_in_ortools:
                    debug_info.append(f"run_ortools: {v.차량번호} - ✅ OR-Tools에서 선택됨 (인덱스: {v_idx})")
                else:
                    debug_info.append(f"run_ortools: {v.차량번호} - ❌ OR-Tools에서 선택되지 않음 (인덱스: {v_idx}, 주문수: {len(orders)}, 선택된차량수: {len(used_vehicle_indices_in_ortools)})")
    else:
        # solution이 None인 경우
        for v_idx, v in enumerate(vehicles):
            if v.차량번호 in ["제주96바7400", "제주96바7403"]:
                debug_info.append(f"run_ortools: {v.차량번호} - ⚠️ solution이 None이어서 확인 불가 (인덱스: {v_idx})")
    
    # 🔹 디버깅 정보를 반환값에 포함 (임시로 print로 출력)
    if debug_info:
        print("\n".join(debug_info))
                
    remaining = [orders[i] for i in range(len(orders)) if i not in fulfilled_indices]
    return routes, remaining

@app.post("/optimize")
def optimize(req: OptimizationRequest):
    # 🔹 디버깅: API 요청 데이터 확인
    debug_info = []
    for v in req.vehicles:
        if v.차량번호 in ["제주96바7400", "제주96바7403"]:
            debug_info.append(f"API요청: {v.차량번호} - 유종: '{v.유종}', 수송용량: {v.수송용량}")
    if debug_info:
        print("\n".join(debug_info))
    
    gas = solve_multitrip_vrp(req.orders, req.vehicles, "휘발유")
    diesel = solve_multitrip_vrp(req.orders, req.vehicles, "등경유")
    
    # 🔹 디버그 로그를 상위 레벨로도 포함 (n8n에서 쉽게 접근)
    all_debug_logs = gas.get("debug_logs", []) + diesel.get("debug_logs", [])
    
    return {
        "gasoline": gas, 
        "diesel": diesel,
        "debug_logs": all_debug_logs,  # 🔹 모든 디버그 로그를 최상위에도 포함
        "debug_summary": {
            "gasoline_logs_count": len(gas.get("debug_logs", [])),
            "diesel_logs_count": len(diesel.get("debug_logs", [])),
            "total_logs_count": len(all_debug_logs)
        }
    }

@app.get("/")
def health():
    return {
        "status": "ok", 
        "matrix_loaded": len(MATRIX_DATA) > 0, 
        "naver_api": bool(NAVER_ID)
    }
