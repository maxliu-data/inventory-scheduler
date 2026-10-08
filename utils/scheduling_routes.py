import json

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest

from utils.inventory_scheduling import InventorySchedulingAgent


scheduling_api = Blueprint("scheduling", __name__)


@scheduling_api.route("/inventory_schedule", methods=["POST"])
def inventory_schedule():
    if not request.is_json:
        return jsonify({"status": "error", "message": "請使用 application/json。"}), 415
    max_bytes = 2 * 1024 * 1024
    if request.content_length is not None and request.content_length > max_bytes:
        return jsonify({"status": "error", "message": "盤點資料不可超過 2 MiB。"}), 413
    try:
        body = request.stream.read(max_bytes + 1)
        if len(body) > max_bytes:
            return jsonify({"status": "error", "message": "盤點資料不可超過 2 MiB。"}), 413
        result = InventorySchedulingAgent().generate(json.loads(body))
    except (ValueError, RecursionError, BadRequest):
        return jsonify({
            "status": "error",
            "message": "盤點資料不正確，請檢查八表資料、日期、群組與店況規則。",
        }), 400
    return jsonify(result)
