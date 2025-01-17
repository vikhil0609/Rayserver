import os
import cv2
import ray
import boto3
import logging
import requests
import numpy as np
import layoutparser as lp
from ray import serve
from dotenv import load_dotenv
from starlette.requests import Request
from paddleocr import PaddleOCR, PPStructure
from label_studio_sdk.utils import parse_config

ray_serve_logger = logging.getLogger("ray.serve")


@serve.deployment(route_prefix="/judgement_pipeline", num_replicas=1, ray_actor_options={'num_gpus': 0.1})
class Translator:
    def __init__(self):
        load_dotenv("/root/Rayserver/.env")
        model_path = "/root/Rayserver/model/model_final.pth"
        config_path = "/root/Rayserver/model/config.yaml"
        hostname = os.getenv("HOST_URL")
        secret_key = os.getenv("VULTR_OBJECT_STORAGE_SECRET_KEY")
        access_key = os.getenv("VULTR_OBJECT_STORAGE_ACCESS_KEY")
        labelstudio_access_token = os.getenv("LABELSTUDIO_API_TOKEN")
        self.labelstudio_api_url = os.getenv("LABELSTUDIO_API_URL")
        self.images_bucket = os.getenv("IMAGES_BUCKET")
        self.headers = {
            "Authorization": f"Token {labelstudio_access_token}",
        }
        session = boto3.session.Session()
        self.client = session.client('s3', **{
            "region_name": hostname.split('.')[0],
            "endpoint_url": "https://" + hostname,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key
        })
        self.model = lp.models.Detectron2LayoutModel(
            config_path,
            model_path,
            extra_config=["MODEL.ROI_HEADS.SCORE_THRESH_TEST", 0.8],
            label_map={0: "extra", 1: "title", 2: "text", 3: "formula",
                       4: "table", 5: "figure", 6: "list"}
        )


    def preprocess(self, data):
        img_array = np.frombuffer(data, dtype=np.uint8)

        # Decode the binary image data using cv2.imdecode
        image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

        return image

    def get_previous_prediction_result_from_labelstudio(self, task_id):
        res = requests.get(self.labelstudio_api_url +
                           f"{task_id}", headers=self.headers)
        if res.status_code == 200:
            ray_serve_logger.info(res.json())
            return res.json()
        else:
            return None

    def get_image_from_s3(self, image_path):
        ray_serve_logger.info(image_path)
        image_name = os.path.basename(image_path)
        bucket_name = image_path.split("//")[1].split("/")[0]

        if bucket_name != self.images_bucket:
            response = self.client.get_object(
            Bucket=bucket_name, Key=image_name)
        else:
            response = self.client.get_object(
                Bucket=self.images_bucket, Key=image_name)
        image_data = response['Body'].read()
        np_array = np.frombuffer(image_data, np.uint8)
        image = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
        return image

    def formatted_model_results(self, from_name, to_name, img_height, img_width, layout_predicted_results):
        results = list()
        score_sum = 0
        for block in layout_predicted_results:
            ray_serve_logger.info(block)
            l = [block.type, block.block.x_1 - 30, block.block.y_1,
                 block.block.x_2 + 10, block.block.y_2]
            _, x, y, xmax, ymax = l
            output_label = block.type
            score_sum += block.score
            results.append({
                'from_name': from_name,
                'to_name': to_name,
                'type': 'rectanglelabels',
                'value': {
                    'rectanglelabels': [output_label],
                    'x': float(x) / img_width * 100,
                    'y': float(y) / img_height * 100,
                    'width': (float(xmax) - float(x)) / img_width * 100,
                    'height': (float(ymax) - float(y)) / img_height * 100,
                },
                'score': block.score,
            })
        avg_score = score_sum / len(results) if score_sum > 0 else 0
        return results, avg_score

    def create_prediction(self, results, task_id, is_prediction_exist, score):
        if is_prediction_exist:
            res = requests.put(self.labelstudio_api_url, headers=self.headers, json={
                "result": results,
                "score": score,
                "model_version": "detectron2",
                "task": task_id
            })
        else:
            res = requests.post(self.labelstudio_api_url, headers=self.headers, json={
                "result": results,
                "score": score,
                "model_version": "detectron2",
                "task": task_id
            })

        return res

    def process_single_task(self, from_name, to_name, task):
        task_id = task.get("id")
        previous_prediction_result = self.get_previous_prediction_result_from_labelstudio(
            task_id=task_id)
        is_prediction_exist = True if previous_prediction_result is not None else False
        data = task.get("data")
        image_path = data.get("image")
        image_data = self.get_image_from_s3(image_path)
        img_width, img_height = image_data.shape[1], image_data.shape[0]
        layout_predicted = self.model.detect(image_data)
        ray_serve_logger.info(layout_predicted)
        results, score = self.formatted_model_results(
            from_name, to_name, img_height, img_width, layout_predicted)
        return results, task_id, is_prediction_exist, score

    async def __call__(self, request: Request):
        if request.url.path == "/health":
            return {
                "status": "ok"
            }
        elif request.url.path == "/setup":
            return {
                "status": "setup done"
            }
        elif request.url.path == "/judgement_pipeline/predict":
            json_data = await request.json()
            ray_serve_logger.info(json_data)
            predictions = []
            results = []
            tasks = json_data.get("tasks")
            label_config = parse_config(json_data.get("label_config"))
            from_name = list(label_config.items())[0][0]
            ray_serve_logger.info(from_name)
            to_name = label_config.get("label").get("to_name")[0]
            for task in tasks:
                ray_serve_logger.info(task)
                results, task_id, is_prediction_exist, score = self.process_single_task(
                    from_name, to_name, task)
                res = self.create_prediction(
                    results, task_id, is_prediction_exist, score)
                ray_serve_logger.info(res.content)
                predictions.append({
                    "result": results,
                    "score": score,
                    "model_version": "something"
                })
            ray_serve_logger.info(predictions)
            return predictions
        else:
            return {"error": "Invalid endpoint"}

# Create and bind the deployment
translator_app = Translator.bind()
serve.run(target=translator_app, host='0.0.0.0')
