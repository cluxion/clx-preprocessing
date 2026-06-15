#!/usr/bin/env python3
"""Concurrency stress test for cluxion_queue with immediate tx and dispatch lock.
Spawns N workers, enqueues M items, dequeues in parallel, verifies exactly-once claims.
"""
import json
import multiprocessing as mp
import tempfile
from pathlib import Path

import cluxion_queue_native

N_WORKERS = 12
N_ITEMS = 200

def run_cmd(cmd, payload):
    return json.loads(cluxion_queue_native.run(cmd, json.dumps(payload)))

def worker(store_dir, claimed, results, errors, worker_id):
    try:
        count = 0
        for _ in range(100):
            res = run_cmd("dequeue", {"store_dir": store_dir})
            if res.get("ready"):
                wid = res["item"]["work_id"]
                claimed.append(wid)
                count += 1
            else:
                break
        results.put((worker_id, count))
    except Exception as e:
        errors.put((worker_id, str(e)))

def main():
    with tempfile.TemporaryDirectory() as tmp:
        store = str(Path(tmp))
        # enqueue N_ITEMS
        for i in range(N_ITEMS):
            wid = f"work-{i:04d}"
            run_cmd("enqueue", {"store_dir": store, "work_id": wid, "prompt": f"test {i}"})

        manager = mp.Manager()
        claimed = manager.list()
        results_q = manager.Queue()
        errors_q = manager.Queue()

        procs = []
        for wid in range(N_WORKERS):
            p = mp.Process(target=worker, args=(store, claimed, results_q, errors_q, wid))
            p.start()
            procs.append(p)

        for p in procs:
            p.join()

        claimed_list = list(claimed)
        unique = set(claimed_list)
        lost = N_ITEMS - len(unique)
        doubles = len(claimed_list) - len(unique)

        print(f"claimed total: {len(claimed_list)} unique: {len(unique)} lost: {lost} doubles: {doubles}")
        if errors_q.qsize() > 0:
            print("errors:", list(errors_q.queue))
        assert lost == 0 and doubles == 0, f"FAIL: lost={lost} doubles={doubles}"
        print("SUCCESS: 0 lost / 0 double / 0 double-dequeue (exact-once)")

if __name__ == "__main__":
    main()
