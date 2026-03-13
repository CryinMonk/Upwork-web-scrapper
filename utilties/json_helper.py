import json

def get_json():
    with open("config.json") as f:
        return json.load(f)

