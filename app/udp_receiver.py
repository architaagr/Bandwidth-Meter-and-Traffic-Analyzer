

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


def receive_udp_traffic(port, run_id, csv_file):

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    sock.bind(("0.0.0.0", port))
    sock.settimeout(2.0)

    received_sequences = set()

    total_datagrams = 0
    unique_packets = 0
    bytes_received = 0
    unique_bytes = 0

    expected_packets = None
    sender_duration = None

    print(f"\nUDP Receiver listening on port {port}...")
    print(f"Experiment ID: {run_id}")
    print("Waiting for incoming packets...\n")

    try:

        while True:

            try:
                data, address = sock.recvfrom(65535)

            except socket.timeout:

                if expected_packets is not None:
                    break

                continue

            if len(data) < HEADER.size:
                continue

            message_type, sequence, timestamp = HEADER.unpack(
                data[:HEADER.size]
            )

            if message_type == b'END!':

                if expected_packets is None:
                    expected_packets = sequence
                    sender_duration = timestamp / 1_000_000_000

                continue

            if message_type != b'DATA':
                continue

            total_datagrams += 1
            bytes_received += len(data)

            if sequence not in received_sequences:

                received_sequences.add(sequence)
                unique_packets += 1
                unique_bytes += len(data)

    except KeyboardInterrupt:
        print("\nReceiver interrupted.")

    finally:

        print("\n========== RECEIVER STATISTICS ==========")

        print(f"Total datagrams received : {total_datagrams}")
        print(f"Unique packets received  : {unique_packets}")
        print(f"Bytes received           : {bytes_received}")

        if expected_packets is not None:

            lost_packets = max(
                0,
                expected_packets - unique_packets
            )

            loss_percentage = (
                lost_packets / expected_packets * 100
                if expected_packets > 0 else 0
            )

            throughput = (
                unique_bytes * 8
            ) / sender_duration / 1_000_000 if sender_duration else 0

            print(f"Packets sent             : {expected_packets}")
            print(f"Packets lost             : {lost_packets}")
            print(f"Packet loss              : {loss_percentage:.2f}%")
            print(f"Application throughput   : {throughput:.3f} Mbps")

        else:

            lost_packets = None
            loss_percentage = None
            throughput = None

            print("Sender's final packet count was not received.")
            print("Exact packet loss cannot be calculated.")

        # Save receiver measurements
        results = {
            "experiment_id": run_id,
            "packets_sent": expected_packets,
            "packets_received": unique_packets,
            "total_datagrams_received": total_datagrams,
            "packets_lost": lost_packets,
            "packet_loss_percent": (
                round(loss_percentage, 3)
                if loss_percentage is not None else ""
            ),
            "bytes_received": unique_bytes,
            "application_throughput_mbps": (
                round(throughput, 3)
                if throughput is not None else ""
            ),
            "sender_duration_seconds": (
                round(sender_duration, 3)
                if sender_duration is not None else ""
            )
        }

        save_to_csv(csv_file, results)

        print(f"\nResults saved to {csv_file}")
        print("=========================================\n")

        sock.close()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="UDP Traffic Receiver"
    )

    parser.add_argument("--port", type=int, default=5001)

    parser.add_argument("--run-id", default="test")
    parser.add_argument("--csv", default="receiver_results.csv")

    args = parser.parse_args()

    receive_udp_traffic(
        args.port,
        args.run_id,
        args.csv
    )

