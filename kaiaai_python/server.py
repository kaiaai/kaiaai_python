import argparse
import asyncio
import json
import logging
import os
import ssl
import uuid

import cv2
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder, MediaRelay
from av import VideoFrame

import threading
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

args_root_path = None
args_image_topic = None
args_cert_file = None
args_key_file = None
args_host = None
args_port = None
args_verbose = False
args_record_to = None

logger = logging.getLogger("pc")
pcs = set()
relay = MediaRelay()


class VideoTransformTrack(MediaStreamTrack):
    """
    A video stream track that transforms frames from an another track.
    """

    kind = "video"

    def __init__(self, track, transform):
        super().__init__()  # don't forget this!
        self.track = track
        self.transform = transform

    async def recv(self):
        # av.VideoFrame yuv420p 640x480
        frame = await self.track.recv()
        img = frame.to_ndarray(format="bgr24")
        ros2_bridge_node.publish_image(img)

        if self.transform == "cartoon":
            # img = frame.to_ndarray(format="bgr24")

            # prepare color
            img_color = cv2.pyrDown(cv2.pyrDown(img))
            for _ in range(6):
                img_color = cv2.bilateralFilter(img_color, 9, 9, 7)
            img_color = cv2.pyrUp(cv2.pyrUp(img_color))

            # prepare edges
            img_edges = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            img_edges = cv2.adaptiveThreshold(
                cv2.medianBlur(img_edges, 7),
                255,
                cv2.ADAPTIVE_THRESH_MEAN_C,
                cv2.THRESH_BINARY,
                9,
                2,
            )
            img_edges = cv2.cvtColor(img_edges, cv2.COLOR_GRAY2RGB)

            # combine color and edges
            img = cv2.bitwise_and(img_color, img_edges)

            # rebuild a VideoFrame, preserving timing information
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        elif self.transform == "edges":
            # perform edge detection
            # img = frame.to_ndarray(format="bgr24")
            img = cv2.cvtColor(cv2.Canny(img, 100, 200), cv2.COLOR_GRAY2BGR)

            # rebuild a VideoFrame, preserving timing information
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        elif self.transform == "rotate":
            # rotate image
            # img = frame.to_ndarray(format="bgr24")
            rows, cols, _ = img.shape
            M = cv2.getRotationMatrix2D((cols / 2, rows / 2), frame.time * 45, 1)
            img = cv2.warpAffine(img, M, (cols, rows))

            # rebuild a VideoFrame, preserving timing information
            new_frame = VideoFrame.from_ndarray(img, format="bgr24")
            new_frame.pts = frame.pts
            new_frame.time_base = frame.time_base
            return new_frame
        else:
            return frame


async def index(request):
    content = open(os.path.join(args_root_path, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


# async def javascript(request):
#     content = open(os.path.join(args_root_path, "client.js"), "r").read()
#     return web.Response(content_type="application/javascript", text=content)


async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pc_id = "PeerConnection(%s)" % uuid.uuid4()
    pcs.add(pc)

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)

    log_info("Created for %s", request.remote)

    # prepare local media
    player = MediaPlayer(os.path.join(args_root_path, "demo-instruct.wav"))
    if args_record_to:
        recorder = MediaRecorder(args_record_to)
    else:
        recorder = MediaBlackhole()

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        log_info("Connection state is %s", pc.connectionState)
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        if track.kind == "audio":
            pc.addTrack(player.audio)
            recorder.addTrack(track)
        elif track.kind == "video":
            pc.addTrack(
                VideoTransformTrack(
                    relay.subscribe(track), transform=params["video_transform"]
                )
            )
            if args_record_to:
                recorder.addTrack(relay.subscribe(track))

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)
            await recorder.stop()

    # handle offer
    await pc.setRemoteDescription(offer)
    await recorder.start()

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        ),
    )


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


class ROS2BridgeNode(Node):
    def __init__(self):
        super().__init__('web_server')

        global args_image_topic, args_cert_file, args_key_file, args_root_path
        global args_host, args_port, args_record_to, args_verbose

        default_root_path = os.path.join(os.path.dirname(__file__), "../public")
        self.declare_parameter('image_topic', '/color_camera/image_raw')
        self.declare_parameter('cert_file', '')
        self.declare_parameter('key_file', '')
        self.declare_parameter('host', '0.0.0.0')
        self.declare_parameter('port', 8080)
        self.declare_parameter('verbose', False)
        self.declare_parameter('record_to', '')
        self.declare_parameter('root_path', default_root_path)

        args_image_topic = self.get_parameter('image_topic').value
        args_cert_file = self.get_parameter('cert_file').value
        args_key_file = self.get_parameter('key_file').value
        args_host = self.get_parameter('host').value
        args_port = self.get_parameter('port').value
        args_verbose = self.get_parameter('verbose').value
        args_record_to = self.get_parameter('record_to').value
        args_root_path = self.get_parameter('root_path').value

        self.publisher_ = self.create_publisher(Image, args_image_topic, 10)
        self.bridge = CvBridge()
    def publish_image(self, img):
        # Input cv::Mat
        self.publisher_.publish(self.bridge.cv2_to_imgmsg(img))
        # self.get_logger().info('Publishing image')


def spin_ros2():
     rclpy.spin(ros2_bridge_node)


def main(args=None):
    rclpy.init(args=args)

    global ros2_bridge_node
    ros2_bridge_node = ROS2BridgeNode()

    ros2_thread = threading.Thread(target=spin_ros2)
    ros2_thread.start()

    # parser = argparse.ArgumentParser(
    #     description="WebRTC audio / video / data-channels demo"
    # )
    # parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    # parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    # parser.add_argument(
    #     "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    # )
    # parser.add_argument(
    #     "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    # )
    # parser.add_argument("--record-to", help="Write received media to a file.")
    # parser.add_argument("--verbose", "-v", action="count")

    # global server_args
    # server_args = parser.parse_args()
    # server_args = argparse.Namespace()
    # server_args.cert_file=None
    # server_args.key_file=None
    # server_args.host='0.0.0.0'
    # server_args.port=8080
    # server_args.record_to=None
    # server_args.verbose=None
    # print(server_args)

    if args_verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args_cert_file and args_key_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args_cert_file, args_key_file)
    else:
        ssl_context = None

    print("Root path: " + args_root_path)

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    # app.router.add_get("/client.js", javascript)
    app.router.add_static('/', path=args_root_path, follow_symlinks=True)
    app.router.add_post("/offer", offer)
    web.run_app(
        app, access_log=None, host=args_host, port=args_port, ssl_context=ssl_context
    )

    ros2_bridge_node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()