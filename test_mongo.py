import certifi, pymongo
from dotenv import load_dotenv
import os

load_dotenv()
uri = os.environ["MONGODB_URI"]
client = pymongo.MongoClient(uri, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
print(client.admin.command("ping"))