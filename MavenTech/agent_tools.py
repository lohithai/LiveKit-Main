"""
MavenTech CRS API Tools — Bus booking via CRS (Computerized Reservation System).
Ported from VideoSDK implementation to standalone async functions for LiveKit.
"""

import json
import os
import re
import time
from datetime import datetime
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from rapidfuzz import process, fuzz

from logger import logger
from database import get_async_context_collection, get_async_collection

load_dotenv()

# ==================== CRS API Configuration ====================

CRS_BASE_URL = (os.getenv("CRS_BASE_URL") or "http://stagingcrsapi2.bookbustickets.com/service.svc").rstrip("/")
CRS_USER_ID = os.getenv("CRS_USER_ID", "")
CRS_KEY_CODE = os.getenv("CRS_KEY_CODE", "")
CRS_COMPANY_ID = os.getenv("CRS_COMPANY_ID", "")
CRS_BRANCH_ID = os.getenv("CRS_BRANCH_ID", "")

# ==================== User Context Cache ====================

_user_context_cache = {}


def get_cached_context(user_id: str) -> Optional[dict]:
    if user_id in _user_context_cache:
        return _user_context_cache[user_id].get("context_data")
    return None


def set_cached_context(user_id: str, context_data: dict):
    _user_context_cache[user_id] = {
        "context_data": context_data.copy(),
        "dirty": False,
        "pending_updates": {},
    }
    logger.info(f"[CACHE] Context cached for user {user_id}")


def update_cached_context(user_id: str, updates: dict):
    if user_id not in _user_context_cache:
        _user_context_cache[user_id] = {
            "context_data": {},
            "dirty": False,
            "pending_updates": {},
        }
    for key, value in updates.items():
        clean_key = key.replace("context_data.", "")
        _user_context_cache[user_id]["context_data"][clean_key] = value
        _user_context_cache[user_id]["pending_updates"][key] = value
    _user_context_cache[user_id]["dirty"] = True
    logger.info(f"[CACHE] Updated cache for {user_id}: {list(updates.keys())}")


async def flush_cached_context(user_id: str) -> bool:
    if user_id not in _user_context_cache:
        return False
    cache_entry = _user_context_cache[user_id]
    if not cache_entry.get("dirty") or not cache_entry.get("pending_updates"):
        logger.info(f"[CACHE] No pending updates for {user_id}")
        clear_cached_context(user_id)
        return True
    try:
        context_collection = await get_async_context_collection()
        await context_collection.update_one(
            {"_id": user_id},
            {"$set": cache_entry["pending_updates"]},
            upsert=True,
        )
        logger.info(f"[CACHE] Flushed {len(cache_entry['pending_updates'])} updates to DB for {user_id}")
        clear_cached_context(user_id)
        return True
    except Exception as e:
        logger.error(f"[CACHE] Failed to flush context for {user_id}: {e}")
        return False


def clear_cached_context(user_id: str):
    if user_id in _user_context_cache:
        del _user_context_cache[user_id]
        logger.info(f"[CACHE] Cleared cache for {user_id}")


# ==================== Cooldown Guard ====================

_tools_called = {}


def _can_call_tool(tool_name: str, cooldown_seconds: float = 5.0) -> bool:
    now = time.time()
    last_call = _tools_called.get(tool_name, 0)
    if now - last_call < cooldown_seconds:
        return False
    _tools_called[tool_name] = now
    return True


# ==================== CRS API Helpers ====================


def _crs_credentials() -> dict:
    resolved = {
        "UserID": CRS_USER_ID,
        "KeyCode": CRS_KEY_CODE,
        "CompanyID": CRS_COMPANY_ID,
        "BranchID": CRS_BRANCH_ID,
    }
    missing = [k for k, v in resolved.items() if not v]
    return {"resolved": resolved, "missing": missing}


def _missing_creds_message(missing: list) -> str:
    return (
        "Booking API credentials are missing: "
        + ", ".join(missing)
        + ". Please set CRS_USER_ID, CRS_KEY_CODE, CRS_COMPANY_ID, CRS_BRANCH_ID in your .env."
    )


async def _crs_get(endpoint: str, params: dict) -> dict:
    url = f"{CRS_BASE_URL}/{endpoint.lstrip('/')}"
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type.lower():
                    payload = await response.json()
                else:
                    text = await response.text()
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {"raw": text}

                if response.status >= 400:
                    return {"success": False, "status_code": response.status, "error": payload, "url": str(response.url)}
                return {"success": True, "data": payload, "url": str(response.url)}
    except Exception as e:
        logger.error(f"CRS GET failed: {e}")
        return {"success": False, "error": str(e), "url": url}


async def _crs_post(endpoint: str, json_data: dict) -> dict:
    url = f"{CRS_BASE_URL}/{endpoint.lstrip('/')}"
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=json_data) as response:
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type.lower():
                    payload = await response.json()
                else:
                    text = await response.text()
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {"raw": text}

                if response.status >= 400:
                    return {"success": False, "status_code": response.status, "error": payload, "url": str(response.url)}
                return {"success": True, "data": payload, "url": str(response.url)}
    except Exception as e:
        logger.error(f"CRS POST failed: {e}")
        return {"success": False, "error": str(e), "url": url}


# ==================== City Resolution (Fuzzy Matching) ====================


async def _resolve_city_id(city_input: str) -> Optional[str]:
    """Resolve city name to CityID using fuzzy matching against the CRS city list."""
    if str(city_input).isdigit():
        return str(city_input)

    creds = _crs_credentials()
    if creds["missing"]:
        logger.error(f"Missing credentials for city resolution: {creds['missing']}")
        return None

    result = await _crs_get("APICompanyCitiesListAll", params=creds["resolved"])
    if not result["success"]:
        logger.error(f"Failed to fetch cities: {result.get('error')}")
        return None

    root = (result["data"] or {}).get("APICompanyCitiesListAllResult") or {}
    cities = root.get("citiesList") or []
    if not cities:
        return None

    city_map = {c.get("CityName"): c.get("CityID") for c in cities if c.get("CityName")}
    city_names = list(city_map.keys())

    match = process.extractOne(str(city_input), city_names, scorer=fuzz.token_sort_ratio)
    if match:
        best_match_name, score, _ = match
        if score > 60:
            logger.info(f"Resolved '{city_input}' -> '{best_match_name}' (ID: {city_map[best_match_name]}, score: {score})")
            return str(city_map[best_match_name])

    logger.warning(f"Could not resolve city '{city_input}'")
    return None


# ==================== CRS Booking Tools ====================


async def booking_get_all_cities() -> str:
    """Fetch all available cities for the company."""
    start = time.time()
    logger.info("[TOOL-START] booking_get_all_cities")
    try:
        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        result = await _crs_get("APICompanyCitiesListAll", params=creds["resolved"])
        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        root = (result["data"] or {}).get("APICompanyCitiesListAllResult") or {}
        cities = root.get("citiesList") or []
        payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "totalCities": len(cities),
            "cities": cities[:50],
        }
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_get_all_cities: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_get_all_cities in {time.time() - start:.3f}s")


async def booking_search_routes(
    from_city: str,
    to_city: str,
    jdate: str,
    max_results: int = 20,
) -> str:
    """Search available routes/trips for a given city pair and journey date.

    Args:
        from_city: City name (e.g. "Bangalore", "Chennai")
        to_city: City name (e.g. "Delhi", "Mumbai")
        jdate: Journey date in YYYY-MM-DD format
        max_results: Maximum number of routes to return
    """
    start = time.time()
    logger.info(f"[TOOL-START] booking_search_routes | {from_city} -> {to_city} on {jdate}")

    if not _can_call_tool("booking_search_routes", cooldown_seconds=5.0):
        return json.dumps({"success": False, "error": "Searching too frequently. Please wait a moment."})

    try:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", (jdate or "")):
            return "Invalid date format. Please use YYYY-MM-DD."

        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        from_city_id = await _resolve_city_id(from_city)
        to_city_id = await _resolve_city_id(to_city)

        if not from_city_id:
            return f"Could not find city matching '{from_city}'. Please check the spelling."
        if not to_city_id:
            return f"Could not find city matching '{to_city}'. Please check the spelling."

        max_results = max(1, min(int(max_results or 20), 50))
        params = {
            **creds["resolved"],
            "FromCityID": str(int(from_city_id)),
            "ToCityID": str(int(to_city_id)),
            "JDate": jdate,
        }

        result = await _crs_get("APISearchRoutesList", params=params)
        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        root = (result["data"] or {}).get("APISearchRoutesListResult") or {}
        routes = root.get("RouteList") or []

        trimmed = []
        for r in routes[:max_results]:
            trip_id_value = r.get("TripID")
            if trip_id_value is not None and not isinstance(trip_id_value, int):
                try:
                    trip_id_value = int(trip_id_value)
                except (ValueError, TypeError):
                    pass

            trimmed.append({
                "TripID": trip_id_value,
                "RouteName": r.get("RouteName"),
                "ServiceName": r.get("ServiceName"),
                "BusType": r.get("BusType"),
                "FromCityName": r.get("FromCityName"),
                "ToCityName": r.get("ToCityName"),
                "FromCityID": r.get("FromCityID"),
                "ToCityID": r.get("ToCityID"),
                "FromCityDepartureTime": r.get("FromCityDepartureTime"),
                "ToCityArivalTime": r.get("ToCityArivalTime"),
                "Availability": r.get("Availability"),
                "SleeperNAC": r.get("SleeperNAC"),
                "SleeperAC": r.get("SleeperAC"),
                "SeaterNAC": r.get("SeaterNAC"),
                "SeaterAC": r.get("SeaterAC"),
                "RouteCode": r.get("RouteCode"),
            })

        payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "totalRoutes": len(routes),
            "routes": trimmed,
        }
        return json.dumps(payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_search_routes: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_search_routes in {time.time() - start:.3f}s")


async def booking_get_available_seats(
    trip_id: int,
    from_city_id: int,
    to_city_id: int,
    journey_date: str,
) -> str:
    """Get available seats and fare details for a specific trip.

    Args:
        trip_id: TripID from search results
        from_city_id: From City ID
        to_city_id: To City ID
        journey_date: Journey date (YYYY-MM-DD)
    """
    start = time.time()
    logger.info(f"[TOOL-START] booking_get_available_seats | TripID: {trip_id}")

    if not _can_call_tool("booking_get_available_seats", cooldown_seconds=3.0):
        return json.dumps({"success": False, "error": "Please wait a moment."})

    try:
        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        params = {
            **creds["resolved"],
            "TripID": str(int(trip_id)),
            "FromCityID": str(from_city_id),
            "ToCityID": str(to_city_id),
            "JDate": journey_date,
        }

        result = await _crs_get("APIGetAvailableSeatsWithFare", params=params)
        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        root = (result["data"] or {}).get("APIGetAvailableSeatsWithFareResult") or {}
        payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "availableSeats": root.get("AvailableSeatData", []),
        }
        return json.dumps(payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_get_available_seats: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_get_available_seats in {time.time() - start:.3f}s")


async def booking_get_pickup_dropoff(route_code: str) -> str:
    """Get pickup and dropoff locations for a route.

    Args:
        route_code: RouteCode from search results
    """
    start = time.time()
    logger.info(f"[TOOL-START] booking_get_pickup_dropoff | RouteCode: {route_code}")
    try:
        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        params = {**creds["resolved"], "RouteCode": route_code}
        result = await _crs_get("APIGetPkpDrp", params=params)
        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        root = (result["data"] or {}).get("APIGetPkpDrpResult") or {}
        payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "pickupPoints": root.get("PickupData", []),
            "dropoffPoints": root.get("DropoffData", []),
        }
        return json.dumps(payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_get_pickup_dropoff: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_get_pickup_dropoff in {time.time() - start:.3f}s")


async def booking_check_availability(
    trip_id: int,
    journey_date: str,
    from_city_id: int,
    to_city_id: int,
) -> str:
    """Check real-time availability of seats in the chart.

    Args:
        trip_id: TripID from search results
        journey_date: Journey date (YYYY-MM-DD)
        from_city_id: From City ID
        to_city_id: To City ID
    """
    start = time.time()
    logger.info(f"[TOOL-START] booking_check_availability | TripID: {trip_id}")

    if not _can_call_tool("booking_check_availability", cooldown_seconds=5.0):
        return json.dumps({"success": False, "error": "Checking availability too fast."})

    try:
        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        params = {
            **creds["resolved"],
            "TripID": str(int(trip_id)),
            "JDate": journey_date,
            "FromCityID": str(from_city_id),
            "ToCityID": str(to_city_id),
        }

        result = await _crs_get("APIGetAvailableSeatsWithFare", params=params)
        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        root = (result["data"] or {}).get("APIGetAvailableSeatsWithFareResult") or {}
        payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "availableSeats": root.get("AvailableSeatData", []),
        }
        return json.dumps(payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_check_availability: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_check_availability in {time.time() - start:.3f}s")


async def booking_create_booking(
    trip_id: int,
    from_city_id: int,
    to_city_id: int,
    journey_date: str,
    pickup_id: int,
    dropoff_id: int,
    total_fare: float,
    primary_passenger_name: str,
    primary_passenger_mobile: str,
    primary_passenger_email: str = "",
    passenger_details_json: str = "[]",
) -> str:
    """Create a bus ticket booking.

    Args:
        trip_id: TripID
        from_city_id: From City ID
        to_city_id: To City ID
        journey_date: YYYY-MM-DD
        pickup_id: Pickup Point ID
        dropoff_id: Dropoff Point ID
        total_fare: Total amount
        primary_passenger_name: Name of primary passenger
        primary_passenger_mobile: Mobile of primary passenger (10 digits)
        primary_passenger_email: Email of primary passenger
        passenger_details_json: JSON string of passenger list.
            Example: '[{"SeatID": "2", "SeatNo": "B2", "Name": "Ravi", "Gender": "M", "Age": 30, "Fare": 550}]'
    """
    start = time.time()
    logger.info(f"[TOOL-START] booking_create_booking | TripID: {trip_id} | Passenger: {primary_passenger_name}")

    # Validation
    validation_errors = []
    if not pickup_id:
        validation_errors.append("pickup_id is required")
    if not dropoff_id:
        validation_errors.append("dropoff_id is required")
    if not primary_passenger_mobile:
        validation_errors.append("primary_passenger_mobile is required")
    if validation_errors:
        return json.dumps({"success": False, "error": f"Missing required fields: {', '.join(validation_errors)}"})

    if not _can_call_tool("booking_create_booking", cooldown_seconds=30.0):
        return json.dumps({"success": False, "error": "Booking is already being processed. Please wait for confirmation."})

    try:
        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        # Validate pickup/dropoff IDs against the route
        try:
            search_params = {
                **creds["resolved"],
                "FromCityID": str(int(from_city_id)),
                "ToCityID": str(int(to_city_id)),
                "JDate": journey_date,
            }
            route_search = await _crs_get("APISearchRoutesList", params=search_params)

            if route_search["success"]:
                route_root = (route_search["data"] or {}).get("APISearchRoutesListResult") or {}
                routes = route_root.get("RouteList") or []
                trip_id_int_val = int(trip_id)

                target_route = None
                for r in routes:
                    try:
                        if int(r.get("TripID", 0)) == trip_id_int_val:
                            target_route = r
                            break
                    except (ValueError, TypeError):
                        pass

                if target_route and target_route.get("RouteCode"):
                    route_code = target_route["RouteCode"]
                    pkp_params = {**creds["resolved"], "RouteCode": route_code}
                    pkp_res = await _crs_get("APIGetPkpDrp", params=pkp_params)

                    if pkp_res["success"]:
                        pkp_root = (pkp_res["data"] or {}).get("APIGetPkpDrpResult") or {}
                        pickup_points = pkp_root.get("PickupData", [])
                        dropoff_points = pkp_root.get("DropoffData", [])

                        valid_pickup_ids = {int(p.get("PickupLocationID", 0)) for p in pickup_points}
                        if int(pickup_id) not in valid_pickup_ids:
                            return json.dumps({"success": False, "error": f"Invalid PickupID: {pickup_id}. Valid IDs: {list(valid_pickup_ids)}"})

                        valid_dropoff_ids = {int(d.get("DropoffLocationID", 0)) for d in dropoff_points}
                        if int(dropoff_id) not in valid_dropoff_ids:
                            return json.dumps({"success": False, "error": f"Invalid DropoffID: {dropoff_id}. Valid IDs: {list(valid_dropoff_ids)}"})

                        logger.info("Pickup and Dropoff IDs validated successfully.")
        except Exception as val_err:
            logger.warning(f"Validation logic failed: {val_err}. Proceeding without validation.")

        # Validate mobile number
        if not re.match(r"^\d{10}$", primary_passenger_mobile):
            return json.dumps({"success": False, "error": "Invalid mobile number. Must be 10 digits."})

        try:
            passengers = json.loads(passenger_details_json)
        except json.JSONDecodeError:
            return json.dumps({"success": False, "error": "Invalid passenger_details_json format."})

        # Construct PassengerDetails strings
        # Format: SeatID~SeatNo~Name~Gender~Age~Fare~Nationality~MobileNo~PassengerID~PassPickUPCharge~PaxDropOffCharge~AppliedSeatTypeFare~IsNACFareApplied
        passenger_details_map = {}
        for i, p in enumerate(passengers[:6]):
            seat_id = str(p.get("SeatID", "0"))
            seat_no = str(p.get("SeatNo", ""))
            name = str(p.get("Name", ""))
            gender = str(p.get("Gender", "M"))
            age = str(p.get("Age", "25"))
            fare = str(p.get("Fare", "0"))
            detail_str = f"{seat_id}~{seat_no}~{name}~{gender}~{age}~{fare}~~{primary_passenger_mobile}~~0~0~0~0"
            passenger_details_map[f"PassengerDetails{i + 1}"] = detail_str

        blank_pattern = "1~~~M~0~0~~~~0~0~0~0"
        for i in range(len(passengers) + 1, 7):
            passenger_details_map[f"PassengerDetails{i}"] = blank_pattern

        trip_id_int = int(trip_id)
        payload_data = {
            **creds["resolved"],
            "BookingID": 0,
            "FromCityID": int(from_city_id),
            "ToCityID": int(to_city_id),
            "JourneyDate": journey_date,
            "TripID": trip_id_int,
            "TotalSeatCount": int(len(passengers)),
            "TotalFare": float(total_fare),
            "Discount": 0.0,
            "SeaterNAC": 0.0,
            "SeaterAC": 0.0,
            "SlumberNAC": 0.0,
            "SlumberAC": 0.0,
            "SleeperNAC": float(total_fare),
            "SleeperAC": 0.0,
            "PrimaryPassengerName": primary_passenger_name,
            "PrimaryPassengerContactNo1": primary_passenger_mobile,
            "PrimaryPassengerContactNo2": "",
            "PrimaryPassengerEmailID": primary_passenger_email,
            **passenger_details_map,
            "PickupID": int(pickup_id),
            "DropoffID": int(dropoff_id),
            "Remarks": "",
            "AutoCancelBeforeJourneyTimeInMinutes": 0,
            "RoundOffFare": 0.0,
            "CouponDiscount": 0.0,
            "CouponID": 0,
            "AutoCancelAfterBookingTimeInMinutes": 0,
            "ChartDate": journey_date,
            "PassengerGSTN": "",
            "SeatCouponId": "",
            "SeatDiscountAmt": "",
            "SeatDiscountSeatNo": "",
        }

        result = await _crs_post("APIBookingsInsertUpdatePhone", json_data=payload_data)

        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        data = result.get("data") or {}
        root = data.get("APIBookingsInsertUpdatePhoneResult") or data

        if isinstance(root, str):
            return json.dumps({"success": False, "error": root})

        booking_status_list = root.get("BookingStatus", [])
        first_booking = booking_status_list[0] if booking_status_list else {}

        # Auto-save to MongoDB
        mongo_id = None
        try:
            db_data = {
                "UserID": creds["resolved"].get("UserID"),
                "CompanyID": creds["resolved"].get("CompanyID"),
                "TripID": trip_id,
                "FromCityID": from_city_id,
                "ToCityID": to_city_id,
                "JourneyDate": journey_date,
                "BookingID": first_booking.get("BookingID", 0),
                "PNR": first_booking.get("PNR", "NA"),
                "TotalFare": total_fare,
                "PassengerName": primary_passenger_name,
                "PassengerMobile": primary_passenger_mobile,
                "PassengerEmail": primary_passenger_email,
                "PickupID": pickup_id,
                "DropoffID": dropoff_id,
                "Passengers": passengers,
                "BookingTime": datetime.utcnow().isoformat(),
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "booking_source": "voice_ai_agent",
                "api_response": root,
            }
            collection = await get_async_collection("bookings")
            insert_res = await collection.insert_one(db_data)
            mongo_id = str(insert_res.inserted_id)
            logger.info(f"Booking saved to MongoDB: {mongo_id}")
        except Exception as db_err:
            logger.error(f"Failed to auto-save booking to MongoDB: {db_err}")

        booking_payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "pnr": str(first_booking.get("PNR", "NA")),
            "bookingId": str(first_booking.get("BookingID", 0)),
            "ticketNumber": str(first_booking.get("SeatNos") or first_booking.get("SeatNo") or ""),
            "totalAmount": str(first_booking.get("TotalAmount") or total_fare),
            "bookingDate": str(first_booking.get("JourneyDateTime") or journey_date),
            "mongodb_id": mongo_id,
        }

        if booking_payload["success"]:
            logger.info(f"Booking created! PNR: {booking_payload['pnr']}")
        else:
            logger.error(f"Booking failed: {booking_payload['errorMessage']}")

        return json.dumps(booking_payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_create_booking: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_create_booking in {time.time() - start:.3f}s")


async def booking_get_city_pairs() -> str:
    """Fetch available city pairs that can be used for searching routes."""
    start = time.time()
    logger.info("[TOOL-START] booking_get_city_pairs")
    try:
        creds = _crs_credentials()
        if creds["missing"]:
            return _missing_creds_message(creds["missing"])

        result = await _crs_get("APIGetCityPairs", params=creds["resolved"])
        if not result["success"]:
            return json.dumps({"success": False, "error": result.get("error")})

        root = (result["data"] or {}).get("APIGetCityPairsResult") or {}
        pairs = root.get("CityPairs") or []
        payload = {
            "success": bool(root.get("status")),
            "errorMessage": root.get("ErrorMessage", ""),
            "totalPairs": len(pairs),
            "cityPairs": pairs[:50],
        }
        return json.dumps(payload, ensure_ascii=False)

    except Exception as e:
        logger.error(f"[TOOL-ERROR] booking_get_city_pairs: {e}")
        return json.dumps({"success": False, "error": str(e)})
    finally:
        logger.info(f"[TOOL-END] booking_get_city_pairs in {time.time() - start:.3f}s")
