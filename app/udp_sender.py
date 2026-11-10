
import socket
import time
import struct
import argparse
import csv
import os

HEADER = struct.Struct("!4sIQ")


def save_to_csv(filename, data):

    file_exists = os.path.exists(filename) and os.path.getsize(filename) > 0

    with open(filename, "a", newline="") as file:

        writer = csv.DictWriter(
            file,
            fieldnames=data.keys()
        )

        if not file_exists:
            writer.writeheader()

        writer.writerow(data)


def send_udp_traffic(destination, port, duration,
                     rate_mbps, packet_size, run_id, csv_file):

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if packet_size < HEADER.size:
        raise ValueError("Packet size must be at least 16 bytes")

    if rate_mbps <= 0 or duration <= 0:
        raise ValueError("Rate and duration must be positive")

    payload_size = packet_size - HEADER.size
    payload = b'X' * payload_size

    bytes_per_second = rate_mbps * 1_000_000 / 8
    packet_interval = packet_size / bytes_per_second

    packets_sent = 0
    bytes_sent = 0

    start_time = time.monotonic()
    next_send_time = start_time

    print("\nStarting UDP traffic generation...")
    print(f"Experiment ID: {run_id}")
    print(f"Destination: {destination}:{port}")
    print(f"Target rate: {rate_mbps} Mbps")
    print(f"Packet size: {packet_size} bytes")
    print(f"Duration: {duration} seconds")

    try:

        while True:

            current_time = time.monotonic()

            if current_time - start_time >= duration:
                break

            if current_time < next_send_time:
                time.sleep(next_send_time - current_time)

            sequence = packets_sent + 1
            timestamp = time.monotonic_ns()

            header = HEADER.pack(
                b'DATA',
                sequence,
                timestamp
            )

            packet = header + payload

            sock.sendto(packet, (destination, port))

            packets_sent += 1
            bytes_sent += len(packet)

            next_send_time += packet_interval

    finally:

        elapsed = time.monotonic() - start_time

        # Send end marker containing the total packet count
        end_packet = HEADER.pack(
            b'END!',
            packets_sent,
            int(elapsed * 1_000_000_000)
        )

        for _ in range(3):
            sock.sendto(end_packet, (destination, port))
            time.sleep(0.05)

        throughput = (bytes_sent * 8) / elapsed / 1_000_000

        print("\n========== SENDER STATISTICS ==========")
        print(f"Packets transmitted : {packets_sent}")
        print(f"Bytes transmitted   : {bytes_sent}")
        print(f"Duration            : {elapsed:.3f} seconds")
        print(f"Sending throughput  : {throughput:.3f} Mbps")

        # Save sender measurements
        results = {
            "experiment_id": run_id,
            "target_rate_mbps": rate_mbps,
            "packet_size_bytes": packet_size,
            "duration_seconds": round(elapsed, 3),
            "packets_sent": packets_sent,
            "bytes_sent": bytes_sent,
            "sender_throughput_mbps": round(throughput, 3)
        }

        save_to_csv(csv_file, results)

        print(f"\nResults saved to {csv_file}")
        print("=======================================\n")

        sock.close()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="UDP Traffic Generator"
    )

    parser.add_argument("--dest", required=True)
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--size", type=int, default=1024)

    parser.add_argument("--run-id", default="test")
    parser.add_argument("--csv", default="sender_results.csv")

    args = parser.parse_args()

    send_udp_traffic(
        args.dest,
        args.port,
        args.duration,
        args.rate,
        args.size,
        args.run_id,
        args.csv
    )

