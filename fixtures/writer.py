import argparse
import json
import time
from datetime import datetime, timezone, timedelta
import psycopg

ap=argparse.ArgumentParser()
ap.add_argument("--dsn",required=True)
ap.add_argument("--count",type=int,default=40)
ap.add_argument("--first-id",type=int,default=1000)
ap.add_argument("--interval",type=float,default=.05)
args=ap.parse_args()
with psycopg.connect(args.dsn, autocommit=True) as connection:
    connection.execute("SET statement_timeout='3s'")
    for n in range(args.count):
        event_id=args.first_id+n
        occurred=datetime(2026,1 if n%2==0 else 2,15,tzinfo=timezone.utc)+timedelta(microseconds=n)
        try:
            connection.execute("INSERT INTO partition_input.events VALUES (%s,%s,%s)",(event_id,occurred,"concurrent-%d"%n))
            print(json.dumps({"id":event_id,"status":"committed"}),flush=True)
        except psycopg.Error as exc:
            print(json.dumps({"id":event_id,"status":"failed","sqlstate":exc.sqlstate}),flush=True)
        time.sleep(args.interval)
