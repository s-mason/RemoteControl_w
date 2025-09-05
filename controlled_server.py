import fractions  
import json
import io
import time
import subprocess
import shlex
import re
# PIL (Pillow) 版本: 9.5.0
from PIL import ImageGrab, Image
# aiohttp 版本: 3.8.6
from aiohttp import web
# aiortc==1.5.0
from aiortc import RTCSessionDescription, MediaStreamTrack, RTCIceCandidate
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
import numpy as np
import av
import asyncio
import uuid
from datetime import datetime
import os
import signal


# 首先定义路由表对象
# routes = web.RouteTableDef()

# 配置
PASSWORD = "666"  # 控制密码
HOST = "0.0.0.0"     # 监听所有网络接口
PORT = 8080          # 服务端口

# 全局变量
pc = None
control_enabled = False
control_channel = None

# Add global variables for alternative communication
active_sessions = {}  # Track active sessions
session_commands = {}  # Store commands for polling
current_session_id = None
last_activity_time = None
INACTIVITY_TIMEOUT = 15 * 60  # 15 minutes in seconds
inactivity_task = None
screen_track = None

# Password attempt limiting
MAX_FAILED_ATTEMPTS = 3
LOCKOUT_DURATION = 300  # 5 minutes in seconds
failed_attempts = 0
lockout_until = None

last_mouse_position = {"x": 0, "y": 0}
current_mouse_x = 640  # Default center x (1280/2)
current_mouse_y = 360  # Default center y (720/2)
mouse_move_queue = []
last_mouse_move_time = 0
MOUSE_MOVE_THROTTLE = 0.005  # 00ms minimum between moves
# Frame rate control
WEBRTC_TARGET_FPS = 30
WEBRTC_TIMESTAMP_INCREMENT = 90000 // WEBRTC_TARGET_FPS  # 4500 for 20 FPS
last_scroll_time = 0
SCROLL_THROTTLE = 0.01  # 10ms minimum between scrolls
pending_candidates = []
# 鼠标点按拖动相关变量
mouse_button_state = {
    'left': False,
    'right': False,
    'middle': False
}
drag_start_x = 0
drag_start_y = 0

last_mouse_down_time = 0
last_mouse_down_position = {"x": 0, "y": 0}
CLICK_THRESHOLD_TIME = 0.5  # 500ms threshold for click detection
CLICK_THRESHOLD_DISTANCE = 5  # 5 pixel threshold for movement

# Add these global variables at the top
xdotool_process = None
xdotool_stdin = None
xdotool_stdout = None

# 配置ICE服务器
ice_servers = [
    RTCIceServer(urls="stun:stun.l.google.com:19302"),
    RTCIceServer(urls="stun:stun1.l.google.com:19302"),
    RTCIceServer(urls="stun:stun2.l.google.com:19302"),
    RTCIceServer(urls="stun:stunserver.org:3478")
]

# 获取显示器序号
def get_active_display():
    """获取活动的DISPLAY"""
    # Try to get from environment first
    display = os.environ.get('DISPLAY')
    if display:
        return display
    
    # Try common values
    for disp in [':1', ':0', ':2']:
        try:
            env = os.environ.copy()
            env['DISPLAY'] = disp
            subprocess.run(
                ['xdotool', 'getdisplaygeometry'],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1
            )
            return disp
        except:
            continue
    
    # Default fallback
    return ':0'

# initialize persistent xdotool
def init_xdotool_process():
    global xdotool_process, xdotool_stdin, xdotool_stdout
    try:
        # Start xdotool in command mode for persistent usage
        xdotool_process = subprocess.Popen(
            ["xdotool", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1
        )
        xdotool_stdin = xdotool_process.stdin
        print("Persistent xdotool process started")
    except Exception as e:
        print(f"Failed to start persistent xdotool: {e}")



# 生成屏幕视频流的类
class ScreenShareTrack(MediaStreamTrack):
    """屏幕共享轨道，负责捕获屏幕并发送给主控端"""
    kind = "video"

    # Add frame counter
    frame_counter = 0

    def __init__(self):
        super().__init__()
        print("[WEBRTC] ScreenShareTrack 轨道已初始化")
        
        # Automatically detect screen dimensions
        try:
            # Try to get screen size using xdotool first
            result = subprocess.run(['xdotool', 'getdisplaygeometry'], 
                                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                dimensions = result.stdout.strip().split()
                if len(dimensions) >= 2:
                    self.original_width = int(dimensions[0])
                    self.original_height = int(dimensions[1])
                    print(f"[WEBRTC] 通过xdotool获取屏幕尺寸: {self.original_width}x{self.original_height}")
                else:
                    raise Exception("xdotool returned unexpected output")
            else:
                # Fallback to PIL if xdotool fails
                img = ImageGrab.grab()
                self.original_width = img.size[0]
                self.original_height = img.size[1]
                print(f"[WEBRTC] 通过PIL获取屏幕尺寸: {self.original_width}x{self.original_height}")
        except Exception as e:
            print(f"[WEBRTC] 获取屏幕尺寸失败，使用默认值: {e}")
            # Default fallback
            self.original_width = 1920
            self.original_height = 1080
                
        print(f"[WEBRTC] 原始屏幕尺寸: {self.original_width}x{self.original_height}")
        # Set target dimensions for transmission - use original dimensions
        self.target_width = self.original_width
        self.target_height = self.original_height
        
        # For WebRTC, we should use a more standard frame rate
        self.frame_rate = 15  # Reduce frame rate to reduce bandwidth
        self.timestamp_increment = int(90000 / self.frame_rate)
        self.timestamp = 0
        # In the __init__ method, after self.timestamp = 0, add:
        self._start_time = time.time()
        # 在 __init__ 方法末尾添加
        self._stop_event = asyncio.Event()


    async def recv(self):

        ScreenShareTrack.frame_counter += 1
        frame_num = ScreenShareTrack.frame_counter
        # print(f"[WEBRTC] === Generating frame #{frame_num} ===")


        frame_num = self.timestamp // self.timestamp_increment
        # print(f"[WEBRTC] === Generating frame #{frame_num} ===")
        
        try:
            # print(f"[WEBRTC] Capturing screen...")
            img = ImageGrab.grab()
            # print(f"[WEBRTC] Screen captured: {img.size}, mode: {img.mode}")
            
            # Force conversion to RGB mode
            if img.mode != 'RGB':
                # print(f"[WEBRTC] Converting image from {img.mode} to RGB")
                img = img.convert('RGB')
            
            # Use actual screen dimensions
            target_width = self.original_width
            target_height = self.original_height
            # print(f"[WEBRTC] Target dimensions: {target_width}x{target_height}")
            
            # Scale down if needed
            if target_width > 1280:
                scale = 1280 / target_width
                target_width = int(target_width * scale)
                target_height = int(target_height * scale)
                # print(f"[WEBRTC] Scaling to: {target_width}x{target_height}")
                img = img.resize((target_width, target_height), Image.LANCZOS)
            
            # Convert to numpy array
            img_np = np.array(img)
            # print(f"[WEBRTC] Converted to numpy array: {img_np.shape}")
            
            # Create VideoFrame
            # print(f"[WEBRTC] Creating VideoFrame from ndarray")
            frame = av.VideoFrame.from_ndarray(img_np, format="rgb24")
            # print(f"[WEBRTC] VideoFrame created: {frame.width}x{frame.height}")
            
            # print(f"[WEBRTC] Reformatting frame to yuv420p")
            frame = frame.reformat(width=target_width, height=target_height, format="yuv420p")
            # print(f"[WEBRTC] Frame reformatted: {frame.width}x{frame.height}")
            
            # Verify frame data
            if frame.width <= 0 or frame.height <= 0:
                raise ValueError(f"Invalid frame dimensions: {frame.width}x{frame.height}")
            


            # Set timestamp properly for WebRTC
            # In the recv() method, replace the timestamp calculation with:
            # 将时间戳计算部分替换为：
            current_time = time.time()
            self.timestamp = int((current_time - self._start_time) * 90000)
            frame.pts = self.timestamp
            frame.time_base = fractions.Fraction(1, 90000)
            
            # 打印时间戳
            # print(f"[WEBRTC] Frame timestamp set: pts={frame.pts}, time_base={frame.time_base}")
            
            # print(f"[WEBRTC] === Frame #{frame_num} generated successfully ===")
            # print(f"[WEBRTC] Frame #{frame_num} details: {frame.width}x{frame.height}, pts={frame.pts}")

            # 在返回 frame 前添加
            if frame.width == 0 or frame.height == 0:
                raise ValueError("Generated frame has invalid dimensions")
        
            return frame
            
        except Exception as e:
            print(f"[WEBRTC] === Frame #{frame_num} generation failed ===")
            print(f"[WEBRTC] Error: {e}")
            import traceback
            traceback.print_exc()
            
            # Create a black frame as fallback
            fallback_width = max(320, self.original_width // 4)
            fallback_height = max(240, self.original_height // 4)
            print(f"[WEBRTC] Creating fallback frame: {fallback_width}x{fallback_height}")
            
            frame = av.VideoFrame(width=fallback_width, height=fallback_height, format="yuv420p")
            luma_size = fallback_width * fallback_height
            chroma_size = (fallback_width // 2) * (fallback_height // 2)
            
            frame.planes[0].update(bytes([0] * luma_size))
            frame.planes[1].update(bytes([128] * chroma_size))
            frame.planes[2].update(bytes([128] * chroma_size))
            
            # In the recv() method, replace the timestamp calculation with:
            current_time = time.time()
            self.timestamp = int((current_time - self._start_time) * 90000)
            frame.pts = self.timestamp
            frame.time_base = fractions.Fraction(1, 90000)
            print(f"[WEBRTC] Fallback frame created: {frame.width}x{frame.height}")
            return frame


class ControlSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.commands = []
        self.last_access = datetime.now()
        self.data_channel = None
        self.pending_mouse_moves = []  # Track pending mouse moves
        
    def add_command(self, command):
        # Special handling for mouse_move commands
        if command.get("type") == "mouse_move":
            # Check if there's already a pending mouse move
            existing_mouse_cmd = None
            for i, cmd in enumerate(self.commands):
                if cmd['command'].get("type") == "mouse_move":
                    existing_mouse_cmd = i
                    break
            
            # If there's a pending mouse move, replace it with the new one
            if existing_mouse_cmd is not None:
                self.commands[existing_mouse_cmd] = {
                    'id': str(uuid.uuid4()),
                    'command': command,
                    'timestamp': datetime.now().isoformat()
                }
            else:
                # No pending mouse move, add normally
                self.commands.append({
                    'id': str(uuid.uuid4()),
                    'command': command,
                    'timestamp': datetime.now().isoformat()
                })
        else:
            # Handle other commands normally
            self.commands.append({
                'id': str(uuid.uuid4()),
                'command': command,
                'timestamp': datetime.now().isoformat()
            })
        
    def get_commands(self):
        commands = self.commands.copy()
        self.commands.clear()
        return commands

async def create_session(request):
    """Create a new control session for alternative communication"""
    global control_enabled, current_session_id, failed_attempts, lockout_until
    
    # Check if we're in lockout period
    if lockout_until and time.time() < lockout_until:
        remaining_time = int(lockout_until - time.time())
        return web.Response(
            status=429,  # Too Many Requests
            content_type="application/json",
            text=json.dumps({
                "error": f"密码尝试次数过多，请 {remaining_time} 秒后再试",
                "retry_after": remaining_time
            })
        )
    
    data = await request.json()
    password = data.get("password", "")
    
    if password != PASSWORD:
        failed_attempts += 1
        if failed_attempts >= MAX_FAILED_ATTEMPTS:
            lockout_until = time.time() + LOCKOUT_DURATION
            return web.Response(
                status=429,
                content_type="application/json",
                text=json.dumps({
                    "error": f"密码尝试次数过多，请 {LOCKOUT_DURATION} 秒后再试",
                    "retry_after": LOCKOUT_DURATION
                })
            )
        
        return web.Response(
            status=401,
            content_type="application/json",
            text=json.dumps({
                "error": f"密码错误，还有 {MAX_FAILED_ATTEMPTS - failed_attempts} 次尝试机会"
            })
        )
    
    # Reset failed attempts on successful login
    failed_attempts = 0
    lockout_until = None
    
    session_id = str(uuid.uuid4())
    session = ControlSession(session_id)
    active_sessions[session_id] = session
    control_enabled = True
    current_session_id = session_id
    
    # Get actual screen dimensions
    width = 1920
    height = 1080
    
    if screen_track:
        width = screen_track.original_width
        height = screen_track.original_height
    else:
        # Try to get actual screen size
        try:
            result = subprocess.run(['xdotool', 'getdisplaygeometry'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                dimensions = result.stdout.strip().split()
                if len(dimensions) >= 2:
                    width = int(dimensions[0])
                    height = int(dimensions[1])
        except:
            pass
    
    # Send actual screen info as first command
    session.add_command({
        "type": "screen_info",
        "width": width,
        "height": height
    })
    
    return web.Response(
        content_type="application/json",
        text=json.dumps({
            "session_id": session_id,
            "status": "success"
        })
    )

async def send_command_http(request):
    """Store control command for HTTP-based communication"""
    try:
        data = await request.json()
        session_id = data.get("session_id")
        command = data.get("command")
        
        if session_id not in active_sessions:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Session not found"})
            )
        
        session = active_sessions[session_id]
        session.add_command(command)
        
        # Handle disconnect command
        if isinstance(command, dict) and command.get("type") == "disconnect":
            restore_screen_and_lock()
        
        # DO NOT send back via WebRTC - just acknowledge receipt
        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "success"})
        )
    except Exception as e:
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"error": str(e)})
        )

async def get_commands_http(request):
    """Get pending commands for HTTP-based communication"""
    try:
        session_id = request.query.get("session_id")
        
        if not session_id or session_id not in active_sessions:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Session not found"})
            )
        
        session = active_sessions[session_id]
        commands = session.get_commands()  # This also clears the commands
        
        # Return the commands that were stored, not send them back
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "commands": commands,
                "status": "success"
            })
        )
    except Exception as e:
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"error": str(e)})
        )
    

# polling and processing HTTP commands
async def process_http_commands():
    """Background task to poll and process HTTP-based commands"""
    global current_session_id
    while True:
        try:
            # Only process if we have a session and no WebRTC channel is active
            if current_session_id and (not control_channel or control_channel.readyState != "open"):
                # Check for pending commands
                if current_session_id in active_sessions:
                    session = active_sessions[current_session_id]
                    commands = session.get_commands()
                    
                    # Process each command
                    for cmd in commands:
                        # print(f"Processing HTTP command: {cmd}")
                        try:
                            # Handle the command (same as WebRTC path)
                            handle_control_command(cmd['command'])
                        except Exception as e:
                            print(f"Error processing HTTP command: {e}")
            
            # Wait before next poll
            await asyncio.sleep(0.01)  # Poll every 10ms
        except Exception as e:
            print(f"Error in HTTP command processing: {e}")
            await asyncio.sleep(1)  # Wait longer on error


async def index(request):
    """提供主控端连接页面"""
    content = open("index.html", "r").read()
    return web.Response(content_type="text/html", text=content)



async def offer(request):
    """处理主控端的offer请求"""
    global pc, control_enabled, control_channel, last_activity_time, inactivity_task, screen_track
    global failed_attempts, lockout_until

    # Check if we're in lockout period
    if lockout_until and time.time() < lockout_until:
        remaining_time = int(lockout_until - time.time())
        return web.Response(
            status=429,  # Too Many Requests
            content_type="application/json",
            text=json.dumps({
                "error": f"密码尝试次数过多，请 {remaining_time} 秒后再试",
                "retry_after": remaining_time
            })
        )
    
    try:
        print("[WEBRTC] 开始处理offer请求")
        
        # 如果已有连接，先关闭它
        if pc:
            print("[WEBRTC] 关闭现有PeerConnection")
            await pc.close()
            pc = None
            control_channel = None

        # Reset inactivity tracking
        last_activity_time = time.time()
        if inactivity_task:
            inactivity_task.cancel()
        inactivity_task = asyncio.create_task(check_inactivity())

        data = await request.json()
        password = data.get("password", "")
        print(f"[WEBRTC] Received offer type: {data.get('type')}")
        print(f"[WEBRTC] Offer SDP length: {len(data.get('sdp', ''))}")
        print(f"[WEBRTC] 收到密码: {'*' * len(password) if password else '无'}")
                
        if password != PASSWORD:
            failed_attempts += 1
            if failed_attempts >= MAX_FAILED_ATTEMPTS:
                lockout_until = time.time() + LOCKOUT_DURATION
                return web.Response(
                    status=429,
                    content_type="application/json",
                    text=json.dumps({
                        "error": f"密码尝试次数过多，请 {LOCKOUT_DURATION} 秒后再试",
                        "retry_after": LOCKOUT_DURATION
                    })
                )
            
            return web.Response(
                status=401,
                content_type="application/json",
                text=json.dumps({
                    "error": f"密码错误，还有 {MAX_FAILED_ATTEMPTS - failed_attempts} 次尝试机会"
                })
            )
    
        # Reset failed attempts on successful login
        failed_attempts = 0
        lockout_until = None
    
        control_enabled = True
        print("[WEBRTC] 控制已启用")
        
        # 创建PeerConnection
        print("[WEBRTC] 创建RTCPeerConnection配置")
        config = RTCConfiguration(iceServers=ice_servers)
        print(f"[WEBRTC] ICE服务器配置: {config.iceServers}")
        
        pc = RTCPeerConnection(configuration=config)
        print("[WEBRTC] 已创建 RTCPeerConnection 实例")
        
        
        # Process any pending candidates
        if pending_candidates:
            print(f"[WEBRTC] Processing {len(pending_candidates)} pending ICE candidates")
            for candidate_data in pending_candidates:
                try:
                    candidate = create_ice_candidate(candidate_data)
                    if candidate:
                        await pc.addIceCandidate(candidate)
                        print(f"[WEBRTC] Added pending ICE candidate: {candidate}")
                except Exception as e:
                    print(f"[WEBRTC] Failed to add pending ICE candidate: {e}")
            pending_candidates.clear()


        # Initialize screen track if not already done
        if screen_track is None:
            print("[WEBRTC] 初始化屏幕轨道")
            screen_track = ScreenShareTrack()
            print("[WEBRTC] 创建新的ScreenShareTrack实例")
            # After screen_track = ScreenShareTrack() add:
            if hasattr(screen_track, '_stop_event'):
                screen_track._stop_event.clear()
        else:
            print("[WEBRTC] 使用现有ScreenShareTrack实例")

        # Add this verification code:
        # Verify ScreenShareTrack validity and print its properties
        if screen_track:
            print(f"[WEBRTC] ScreenShareTrack 验证信息:")
            print(f"  - ID: {getattr(screen_track, 'id', 'N/A')}")
            print(f"  - Kind: {getattr(screen_track, 'kind', 'N/A')}")
            print(f"  - ReadyState: {getattr(screen_track, 'readyState', 'N/A')}")
            
            # Check if it has required methods and attributes
            has_recv_method = hasattr(screen_track, 'recv')
            has_stop_method = hasattr(screen_track, 'stop')
            print(f"  - Has recv() method: {has_recv_method}")
            print(f"  - Has stop() method: {has_stop_method}")
            
            if not has_recv_method:
                print("[WEBRTC] 警告: ScreenShareTrack 缺少 recv() 方法")
        else:
            print("[WEBRTC] 错误: ScreenShareTrack 未正确初始化")


        # Create data channel first
        control_channel = pc.createDataChannel("control-channel")
        setup_data_channel_events(control_channel)
        print("[WEBRTC] Created control data channel")
            
        # Add transceiver for video streaming with explicit direction
        print("[WEBRTC] 添加视频传输器")
        # In the offer() function, enhance the transceiver creation logging:
        try:
            # Create transceiver with explicit direction
            print("[WEBRTC] Creating video transceiver with sendonly direction")
            transceiver = pc.addTransceiver("video",track=screen_track, direction="sendonly")
            await asyncio.sleep(0.1)  # Wait for transceiver to initialize
            # Attach the track to the sender
            # if screen_track:
            #     await transceiver.sender.replaceTrack(screen_track)
            # Properly initialize transceiver directions for aiortc 1.5.0
            transceiver._offerDirection = "sendonly"
            transceiver.direction = "sendonly"

            # For aiortc 1.5.0, also set currentDirection if it's None
            if transceiver.currentDirection is None:
                transceiver.currentDirection = "sendonly"

            # 在 transceiver.sender.replaceTrack(screen_track) 后添加
            if hasattr(transceiver, 'setDirection'):
                transceiver.setDirection("sendonly")
            print(f"[WEBRTC] Added screen share track, track ID: {screen_track.id}")
         
            print(f"[WEBRTC] 已添加屏幕共享轨道，轨道ID: {screen_track.id}")
            # Log detailed transceiver info
            print(f"[WEBRTC] Transceiver details after track replacement:")
            print(f"  - Kind: {transceiver.kind}")
            print(f"  - Direction: {transceiver.direction}")
            print(f"  - Current direction: {transceiver.currentDirection}")
            print(f"  - Mid: {transceiver.mid}")
            print(f"  - Sender track: {transceiver.sender.track}")


            if transceiver.receiver:
                print(f"  - Receiver track: {transceiver.receiver.track}")
            else:
                print(f"  - Receiver track: None")
            
            print(f"[WEBRTC] Added screen share track, track ID: {screen_track.id}")


            # Add this verification:
            # Additional verification of ScreenShareTrack after transceiver setup
            if screen_track:
                print(f"[WEBRTC] ScreenShareTrack 状态验证:")
                print(f"  - ID: {getattr(screen_track, 'id', 'N/A')}")
                print(f"  - Kind: {getattr(screen_track, 'kind', 'N/A')}")
                print(f"  - ReadyState: {getattr(screen_track, 'readyState', 'N/A')}")
                
                # Verify it's in live state
                if hasattr(screen_track, 'readyState') and screen_track.readyState != "live":
                    print(f"[WEBRTC] 警告: ScreenShareTrack 不在 live 状态，当前状态: {screen_track.readyState}")
                elif hasattr(screen_track, 'readyState'):
                    print(f"[WEBRTC] ScreenShareTrack 状态正常: {screen_track.readyState}")

            # Add this verification:
            print(f"[WEBRTC] Verifying transceiver association:")
            print(f"  - Sender track ID: {transceiver.sender.track.id if transceiver.sender.track else 'None'}")
            print(f"  - Screen track ID: {screen_track.id}")
            if transceiver.sender.track == screen_track:
                print("[WEBRTC] Transceiver properly associated with media stream")
            else:
                print("[WEBRTC] ERROR: Transceiver NOT properly associated with media stream")
        except Exception as e:
            print(f"[WEBRTC] Failed to add transceiver: {e}")
            import traceback
            traceback.print_exc()
            # Fallback method for even older versions
            try:
                pc.addTrack(screen_track)
                print(f"[WEBRTC] 使用addTrack方法添加轨道，轨道ID: {screen_track.id}")
            except Exception as e2:
                print(f"[WEBRTC] Fallback添加轨道也失败: {e2}")
        
        # Get actual screen dimensions for screen info
        screen_width = 1920
        screen_height = 1080
        
        if screen_track:
            screen_width = screen_track.original_width
            screen_height = screen_track.original_height
            print(f"[WEBRTC] 获取屏幕轨道尺寸: {screen_width}x{screen_height}")
        else:
            # Try to get actual screen size
            try:
                result = subprocess.run(['xdotool', 'getdisplaygeometry'], 
                                    capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    dimensions = result.stdout.strip().split()
                    if len(dimensions) >= 2:
                        screen_width = int(dimensions[0])
                        screen_height = int(dimensions[1])
                        print(f"[WEBRTC] 通过xdotool获取屏幕尺寸: {screen_width}x{screen_height}")
            except Exception as e:
                print(f"[WEBRTC] 获取屏幕尺寸时出错: {e}")
                pass

     
        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                # Buffer candidates until channel is ready
                candidate_data = {
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex
                }
                
                # Try to send immediately if channel is open
                if control_channel and control_channel.readyState == "open":
                    try:
                        control_channel.send(json.dumps({
                            "type": "ice_candidate",
                            "data": candidate_data
                        }))
                    except Exception as e:
                        print(f"[WEBRTC] Failed to send ICE candidate: {e}")
                        pending_candidates.append(candidate_data)  # Buffer if failed
                else:
                    pending_candidates.append(candidate_data)  # Buffer if channel not ready

        # 监听ICE连接状态变化
        @pc.on("iceconnectionstatechange")
        def on_iceconnectionstatechange():
            global control_channel
            print(f"[WEBRTC] ICE连接状态: {pc.iceConnectionState}")
            if pc.iceConnectionState == "connected":
                print("[WEBRTC] ICE连接已建立")
            elif pc.iceConnectionState == "failed":
                print("[WEBRTC] ICE连接失败")
                control_channel = None
            elif pc.iceConnectionState == "disconnected":
                print("[WEBRTC] ICE连接断开")
                control_channel = None
            elif pc.iceConnectionState == "closed":
                print("[WEBRTC] ICE连接已关闭")
                control_channel = None

        # 监听连接状态变化
        @pc.on("connectionstatechange")
        def on_connectionstatechange():
            print(f"[WEBRTC] 连接状态变化: {pc.connectionState}")
            if pc.connectionState == "connected":
                print("[WEBRTC] 连接已建立")
            elif pc.connectionState == "failed":
                print("[WEBRTC] 连接失败")
            elif pc.connectionState == "disconnected":
                print("[WEBRTC] 连接断开")
            elif pc.connectionState == "closed":
                print("[WEBRTC] 连接已关闭")

        # 监听信令状态变化
        @pc.on("signalingstatechange")
        def on_signalingstatechange():
            print(f"[WEBRTC] 信令状态: {pc.signalingState}")

        # 监听数据通道创建
        @pc.on("datachannel")
        def on_datachannel(channel):
            global control_channel
            print(f"[WEBRTC] 收到数据通道: {channel.label}")
            control_channel = channel
            setup_data_channel_events(channel)


        # 设置远程描述（来自浏览器的offer）
        offer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
        print(f"[WEBRTC] 收到offer SDP长度: {len(offer.sdp)}")
        # print("=== 浏览器发送的 Offer SDP ===")
        # print(offer.sdp)
        # print("==============================")
        await pc.setRemoteDescription(offer)
        print("[WEBRTC] 已设置远程描述（来自浏览器的offer）")


        # Add after setting remote description and before creating answer:
        await asyncio.sleep(0.1)  # Allow time for transceiver negotiation

        # Verify transceivers before creating answer
        transceivers = pc.getTransceivers()
        print(f"[WEBRTC] Number of transceivers before creating answer: {len(transceivers)}")
        for i, t in enumerate(transceivers):
            print(f"[WEBRTC] Checking transceiver {i} before answer creation:")
            print(f"  - Kind: {t.kind}")
            print(f"  - Direction: {t.direction}")
            print(f"  - _OfferDirection: {t._offerDirection}")
            print(f"  - Current direction: {t.currentDirection}")
            
            # Fix None directions
            if t.direction is None:
                t.direction = "inactive"
            if t._offerDirection is None:
                t._offerDirection = t.direction
            if t.currentDirection is None:
                # For aiortc 1.5.0, set a valid currentDirection based on direction
                if t.direction in ["sendonly", "sendrecv", "recvonly", "inactive"]:
                    print ("[WEBRTC] t.direction: {t.direction}")
                else:
                    t.currentDirection = "inactive"


        # 创建答案
        print("[WEBRTC] 开始创建answer...")
        answer = await pc.createAnswer()

        await pc.setLocalDescription(answer)
        print(f"[WEBRTC] 已设置本地描述")

        # Wait for ICE gathering to complete
        await asyncio.sleep(0.5)

        # Add after creating the answer and before returning:
        print("[WEBRTC] Checking answer SDP for m-line and mid information:")
        if hasattr(answer, 'sdp') and answer.sdp:
            sdp_lines = answer.sdp.split('\n')
            for i, line in enumerate(sdp_lines):
                if line.startswith('m=') or line.startswith('a=mid:'):
                    print(f"[WEBRTC] Answer SDP Line {i}: {line.strip()}")

        if answer is None or not hasattr(answer, 'sdp'):
            raise Exception("Failed to create valid answer")
        print(f"[WEBRTC] Answer创建成功，类型: {answer.type}")
        print(f"[WEBRTC] Answer SDP长度: {len(answer.sdp)}")

        # After await pc.setLocalDescription(answer):
        if pc.localDescription and pc.localDescription.sdp:
            # Verify SDP contains valid candidates
            if "candidate:" not in pc.localDescription.sdp:
                print("[WEBRTC] 警告: SDP中未找到有效的ICE候选者")


        # Add this code to ensure PeerConnection is ready:
        # Wait for ICE gathering to complete or timeout
        ice_gathering_timeout = 5.0  # 5 seconds timeout
        ice_gathering_start = time.time()

        while pc.iceGatheringState != "complete" and (time.time() - ice_gathering_start) < ice_gathering_timeout:
            print(f"[WEBRTC] 等待ICE收集完成: 当前状态={pc.iceGatheringState}")
            await asyncio.sleep(0.1)

        print(f"[WEBRTC] ICE收集状态: {pc.iceGatheringState}")

        # Verify PeerConnection is ready
        if pc.signalingState not in ["stable", "have-local-offer", "have-remote-offer"]:
            print(f"[WEBRTC] 警告: PeerConnection状态异常: {pc.signalingState}")

        # Add this line after setLocalDescription:
        if pc.localDescription:
            print("[WEBRTC] Local SDP after setting local description:")
            debug_sdp(pc.localDescription.sdp, "LOCAL")

        # After creating the answer:
        print("[WEBRTC] Answer SDP:")
        debug_sdp(answer.sdp, "ANSWER")


        # Add this verification code:
        # Verify PeerConnection is in a ready state
        if pc.connectionState not in ["new", "connecting", "connected"]:
            print(f"[WEBRTC] 警告: PeerConnection连接状态: {pc.connectionState}")
            
        if pc.iceConnectionState not in ["new", "checking", "connected", "completed"]:
            print(f"[WEBRTC] 警告: ICE连接状态: {pc.iceConnectionState}")
            
        print(f"[WEBRTC] PeerConnection准备状态检查:")
        print(f"  - 连接状态: {pc.connectionState}")
        print(f"  - ICE连接状态: {pc.iceConnectionState}")
        print(f"  - ICE收集状态: {pc.iceGatheringState}")
        print(f"  - 信令状态: {pc.signalingState}")

        response_data = {
            "sdp": pc.localDescription.sdp if pc.localDescription else "",
            "type": pc.localDescription.type if pc.localDescription else ""
        }
        # print(f"[WEBRTC] 发送响应数据: {response_data}")
        return web.Response(
            content_type="application/json",
            text=json.dumps(response_data)
        )
    except Exception as e:
        print(f"[WEBRTC] 处理offer请求出错: {e}")
        import traceback
        traceback.print_exc()
        
        if pc:
            try:
                await pc.close()
                pc = None
                control_channel = None
            except Exception as close_error:
                print(f"[WEBRTC] 关闭PeerConnection时出错: {close_error}")
        return web.Response(
            status=500,
            content_type="application/json",
            text=json.dumps({"error": str(e)})
        )

# Add a method to check if the screen track is working
def check_screen_track_status():
    """Check and log the status of the screen track"""
    global screen_track
    if screen_track:
        print(f"[DEBUG] ScreenTrack status:")
        print(f"  - Original dimensions: {screen_track.original_width}x{screen_track.original_height}")
        print(f"  - Target dimensions: {screen_track.target_width}x{screen_track.target_height}")
        print(f"  - Frame rate: {screen_track.frame_rate}")
        print(f"  - Timestamp increment: {screen_track.timestamp_increment}")
        print(f"  - Current timestamp: {screen_track.timestamp}")
    else:
        print("[DEBUG] ScreenTrack is None")

# You can call this method periodically or before creating offers
def setup_data_channel_events(channel):
    """设置数据通道事件处理"""
    @channel.on("open")
    def on_open():
        print("[WEBRTC] 数据通道已打开")
        # Send screen info when data channel opens
        screen_info_msg = {
            "type": "screen_info",
            "width": getattr(screen_track, 'original_width', 1920),
            "height": getattr(screen_track, 'original_height', 1080)
        }
        try:
            channel.send(json.dumps(screen_info_msg))
            print("[WEBRTC] 已发送屏幕信息")
        except Exception as e:
            print(f"[WEBRTC] 发送屏幕信息失败: {e}")

    @channel.on("message")
    def on_message(message):
        try:
            command = json.loads(message)
            # print(f"[WEBRTC] 收到控制命令: {command}")
            handle_control_command(command)
        except Exception as e:
            print(f"[WEBRTC] 处理控制命令出错: {e}")

    @channel.on("close")
    def on_close():
        print("[WEBRTC] 数据通道已关闭")
        global control_channel
        control_channel = None

    @channel.on("error")
    def on_error(error):
        print(f"[WEBRTC] 数据通道错误: {error}")
        global control_channel
        control_channel = None

# In screen_update function, ensure consistent sizing:
async def screen_update(request):
    """提供屏幕更新的HTTP端点"""
    if not control_enabled:
        return web.Response(status=403)
    
    try:
        # Capture screen
        img = ImageGrab.grab()
        
        # Use actual screen dimensions from screen_track if available
        if screen_track:
            target_width = screen_track.original_width
            target_height = screen_track.original_height
        else:
            # Use actual screen dimensions
            screen_width, screen_height = img.size
            target_width = screen_width
            target_height = screen_height
            
        # Scale down for HTTP streaming to reduce bandwidth (about 640x360)
        max_width = 1680
        max_height = 945
        
        if target_width > max_width or target_height > max_height:
            scale_x = max_width / target_width
            scale_y = max_height / target_height
            scale = min(scale_x, scale_y)
            target_width = int(target_width * scale)
            target_height = int(target_height * scale)
        
        # Resize with actual dimensions
        img = img.resize((target_width, target_height), Image.LANCZOS)
        
        # Convert to JPEG with lower quality to reduce bandwidth
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=60, optimize=True)  # Reduced quality from 75 to 50
        buffer.seek(0)
        
        return web.Response(body=buffer, content_type="image/jpeg")
    except Exception as e:
        print(f"Screen update error: {e}")
        return web.Response(status=500)

# Add this function to initialize persistent xdotool
def init_xdotool_process():
    global xdotool_process, xdotool_stdin, xdotool_stdout
    try:
        # Prepare environment with proper DISPLAY
        env = os.environ.copy()
        env['DISPLAY'] = os.environ.get('DISPLAY', ':0')
        
        # Start xdotool in command mode for persistent usage
        xdotool_process = subprocess.Popen(
            ["xdotool", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env  # Pass the environment with correct DISPLAY
        )
        xdotool_stdin = xdotool_process.stdin
        print("Persistent xdotool process started with DISPLAY:", env['DISPLAY'])
    except Exception as e:
        print(f"Failed to start persistent xdotool: {e}")
# Modify run_xdotool_command to use persistent process when possible
def run_xdotool_command(command):
    """执行xdotool命令 - optimized version"""
    global xdotool_process, xdotool_stdin
    
    try:
        # For mouse moves, use the persistent process
        if command.startswith("xdotool mousemove_relative"):
            if xdotool_stdin and not xdotool_stdin.closed:
                try:
                    # Extract coordinates from command
                    parts = command.split()
                    if len(parts) >= 3:
                        x, y = parts[1], parts[2]
                        cmd = f"mousemove_relative {x} {y}\n"
                        xdotool_stdin.write(cmd)
                        xdotool_stdin.flush()
                        return subprocess.CompletedProcess(command, 0)
                except Exception as e:
                    print(f"Persistent xdotool failed: {e}")
                    # Fall back to regular method
                    pass
        
        # Regular method for other commands but still optimized
        env = os.environ.copy()
        env['DISPLAY'] = os.environ.get('DISPLAY', ':0')  # Use detected DISPLAY
        
        cmd_parts = shlex.split(command)
        result = subprocess.run(
            cmd_parts,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            timeout=0.1
        )
        
        return result
    except subprocess.TimeoutExpired:
        print(f"xdotool命令超时: {command}")
        return None
    except Exception as e:
        print(f"执行命令时出错: {e}")
        return None

# 主控端
def handle_control_command(command):
    """处理主控端发送的控制指令"""
    global control_channel, current_session_id, last_mouse_move_time, last_mouse_position
    global current_mouse_x, current_mouse_y , last_scroll_time
    # Note: for HTTP mode, control_enabled check is not needed as it's checked at request level
    
    cmd_type = command.get("type")
    # print(f"处理指令: {cmd_type}，参数: {command}")
      
    try:
        if cmd_type == "mouse_init":
            # Initialize mouse position on controlled end
            x, y = command.get("x"), command.get("y")
            current_mouse_x = x
            current_mouse_y = y
            
            # Move mouse to initial position
            result = run_xdotool_command(f"xdotool mousemove {x} {y}")
            if result and result.returncode == 0:
                print(f"鼠标初始化位置成功: ({x}, {y})")
            else:
                print(f"鼠标初始化位置失败: {result.stderr if result else 'Unknown error'}")
            
        elif cmd_type == "request_screen_info":
            # Send screen info immediately
            screen_info_msg = {
                "type": "screen_info",
                "width": getattr(screen_track, 'original_width', 1920) if 'screen_track' in globals() else 1920,
                "height": getattr(screen_track, 'original_height', 1080) if 'screen_track' in globals() else 1080
            }
            
            # Try primary data channel first (WebRTC)
            if control_channel and control_channel.readyState == "open":
                try:
                    control_channel.send(json.dumps(screen_info_msg))
                    print("已发送屏幕尺寸信息响应")
                except Exception as e:
                    print(f"发送屏幕尺寸信息失败: {e}")
            else:
                # If WebRTC is not available, store for HTTP polling
                if current_session_id and current_session_id in active_sessions:
                    session = active_sessions[current_session_id]
                    session.add_command(screen_info_msg)
                    print("存储屏幕尺寸信息用于HTTP轮询")

    
        elif cmd_type == "mouse_move":
            x, y = command.get("x"), command.get("y")
            current_time = time.time()
            # Throttle mouse movements to prevent overload
            if current_time - last_mouse_move_time < MOUSE_MOVE_THROTTLE-0.003:
                last_mouse_position["x"] = x
                last_mouse_position["y"] = y
            else:
                last_mouse_move_time = current_time
            
                # Update last position
                last_mouse_position["x"] = x
                last_mouse_position["y"] = y

                # Use absolute positioning for better accuracy and speed
                # Suppress output for better performance
                try:
                    subprocess.run(
                        ["xdotool", "mousemove_relative", str(x), str(y)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=0.1  # Very short timeout
                    )
                except subprocess.TimeoutExpired:
                    print(f"鼠标移动超时: {x}, {y}")
                except Exception as e:
                    print(f"鼠标移动失败: {e}")
            
        elif cmd_type == "mouse_click":
            button = command.get("button", "left")
            x, y = command.get("x"), command.get("y")
            button_code = "1" if button == "left" else "3" if button == "right" else "2"
            # print(f"执行鼠标点击: button={button}, code={button_code}")
            
            try:
                # Increase timeout and add better error handling
                env = os.environ.copy()
                env['DISPLAY'] = os.environ.get('DISPLAY', ':0')
                
                result = subprocess.run(
                    ["xdotool", "click", button_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    timeout=2.0  # Increase timeout to 2 seconds
                )
                
                if result.returncode != 0:
                    print(f"鼠标点击失败: return code {result.returncode}, stderr: {result.stderr}")
            except subprocess.TimeoutExpired:
                print(f"鼠标点击超时: button={button}, code={button_code}")
            except Exception as e:
                print(f"鼠标点击异常: {e}")
            

        elif cmd_type == "mouse_down":
            button = command.get("button", "left")
            x, y = command.get("x"), command.get("y")
            
            # Store mouse down information for click detection
            last_mouse_down_time = time.time()
            last_mouse_down_position["x"] = x
            last_mouse_down_position["y"] = y

            # Update button state
            mouse_button_state[button] = True
            drag_start_x = x
            drag_start_y = y
            
            # Move mouse to position and press button
            button_code = "1" if button == "left" else "3" if button == "right" else "2"
            try:
                # Move to position first
                subprocess.run(
                    ["xdotool", "mousemove_relative", str(x), str(y)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.1
                )
                # Then press button
                subprocess.run(
                    ["xdotool", "mousedown", button_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.1
                )
                print(f"鼠标按下 {button} 按钮在位置 ({x}, {y})")
            except Exception as e:
                print(f"鼠标按下失败: {e}")

        elif cmd_type == "mouse_up":
            button = command.get("button", "left")
            x, y = command.get("x"), command.get("y")
            
            # Update button state
            mouse_button_state[button] = False

            # Update button state
            mouse_button_state[button] = False
            
            # Move mouse to position and release button
            button_code = "1" if button == "left" else "3" if button == "right" else "2"
            try:
                # Move to position first
                subprocess.run(
                    ["xdotool", "mousemove_relative", str(x), str(y)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.1
                )
                # Then release button
                subprocess.run(
                    ["xdotool", "mouseup", button_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.1
                )

                # Check if this constitutes a click (pressed and released quickly at same location)
                current_time = time.time()
                time_diff = current_time - last_mouse_down_time
                distance = ((x - last_mouse_down_position["x"]) ** 2 + 
                        (y - last_mouse_down_position["y"]) ** 2) ** 0.5
                
                if (time_diff <= CLICK_THRESHOLD_TIME and 
                    distance <= CLICK_THRESHOLD_DISTANCE and
                    last_mouse_down_position["x"] == drag_start_x and
                    last_mouse_down_position["y"] == drag_start_y):
                    # This is a click - send click acknowledgment
                    print(f"检测到鼠标点击 {button} 按钮在位置 ({x}, {y})")
                else:
                    print(f"鼠标释放 {button} 按钮在位置 ({x}, {y})")
                    
            except Exception as e:
                print(f"鼠标释放失败: {e}")

        elif cmd_type == "mouse_drag":
            x, y = command.get("x"), command.get("y")
            start_x, start_y = command.get("startX"), command.get("startY")
            
            try:
                # For drag operations, we move the mouse while button is pressed
                subprocess.run(
                    ["xdotool", "mousemove_relative", str(x), str(y)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.1
                )
                print(f"鼠标拖拽到位置 ({x}, {y})，起始位置 ({start_x}, {start_y})")
            except Exception as e:
                print(f"鼠标拖拽失败: {e}")


        elif cmd_type == "mouse_scroll":
            dy = command.get("dy", 0)
            if dy != 0:
                current_time = time.time()
                # Throttle scroll events to prevent overload
                if current_time - last_scroll_time >= SCROLL_THROTTLE:
                    last_scroll_time = current_time
                    
                    # Use relative scroll wheel events for better performance
                    scroll_amount = int(abs(dy))
                    # Limit scroll amount to prevent excessive scrolling
                    scroll_amount = min(scroll_amount, 10)
                    
                    if scroll_amount > 0:
                        direction = 'up' if dy > 0 else 'down'
                        button_code = '4' if dy > 0 else '5'  # 4=up, 5=down in X11
                        
                        # print(f"执行鼠标滚动: dy={dy}, amount={scroll_amount}, direction={direction}")
                        
                        try:
                            # Use single xdotool command with minimal delay for better performance
                            env = os.environ.copy()
                            env['DISPLAY'] = os.environ.get('DISPLAY', ':0')
                            
                            # Execute scroll command more efficiently
                            result = subprocess.run(
                                ["xdotool", "click", "--repeat", str(scroll_amount), "--delay", "5", button_code],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                text=True,
                                env=env,
                                timeout=0.5  # Reduced timeout
                            )
                            
                            if result.returncode != 0:
                                print(f"鼠标滚动失败: return code {result.returncode}")
                        except subprocess.TimeoutExpired:
                            print(f"鼠标滚动超时")
                        except Exception as e:
                            print(f"鼠标滚动失败: {e}")
                else:
                    print(f"滚动事件被节流: {current_time - last_scroll_time:.4f}s")
            
        elif cmd_type == "key_press":
            key = command.get("key")
            key_mapping = {
                "space": "space",
                "enter": "Return",
                "backspace": "BackSpace",
                "tab": "Tab",
                "escape": "Escape",
                "shift": "Shift",
                "ctrl": "Control",
                "alt": "Alt",
                "caps_lock": "Caps_Lock",
                "delete": "Delete",
                "up": "Up",
                "down": "Down",
                "left": "Left",
                "right": "Right",
                "home": "Home",
                "end": "End",
                "page_up": "Page_Up",
                "page_down": "Page_Down"
            }
            
            xdotool_key = key_mapping.get(key, key)
            print(f"执行按键: key={key}, xdotool_key={xdotool_key}")
            result = run_xdotool_command(f"xdotool key {xdotool_key}")
            if result and result.returncode == 0:
                print(f"按键 {key} ({xdotool_key}) 成功")
            else:
                print(f"按键失败: {result.stderr if result else 'Unknown error'}")
            
        elif cmd_type == "key_type":
            text = command.get("text")
            if text:
                escaped_text = shlex.quote(text)
                print(f"执行文本输入: text={text}")
                result = run_xdotool_command(f"xdotool type {escaped_text}")
                if result and result.returncode == 0:
                    print(f"输入文本 '{text}' 成功")
                else:
                    print(f"输入文本失败: {result.stderr if result else 'Unknown error'}")
        
        # 打开或关闭屏幕
        elif cmd_type == "screen_control":
            action = command.get("action")
            if action in ["on", "off"]:
                success = control_screen(action)
                send_acknowledgment(cmd_type, action=action, success=success)
            else:
                print(f"Invalid screen control action: {action}")
        
        elif cmd_type == "lock_screen":
            success = lock_session()
            send_acknowledgment(cmd_type, success=success)

        elif cmd_type == "disconnect":
            # Handle disconnect command
            restore_screen_and_lock()
            send_acknowledgment(cmd_type, success=True)

        # Send general acknowledgment for other commands
        if cmd_type not in ["screen_control", "lock_screen", "disconnect"]:
            send_acknowledgment(cmd_type)
             
    except Exception as e:
        print(f"处理控制指令出错: {e}")
        import traceback
        traceback.print_exc()
        send_error_message(cmd_type, str(e))

def send_acknowledgment(cmd_type, **kwargs):
    """Send acknowledgment via appropriate channel"""
    ack_msg = {
        "type": "ack", 
        "command": cmd_type
    }
    ack_msg.update(kwargs)
    
    # Try primary data channel first (WebRTC)
    if control_channel and control_channel.readyState == "open":
        try:
            control_channel.send(json.dumps(ack_msg))
            # print(f"发送确认消息: {json.dumps(ack_msg)}")
        except Exception as e:
            print(f"发送确认消息失败: {e}")
    else:
        # If WebRTC is not available, store the acknowledgment for HTTP polling
        if current_session_id and current_session_id in active_sessions:
            session = active_sessions[current_session_id]
            session.add_command(ack_msg)
            # print(f"存储确认消息用于HTTP轮询: {json.dumps(ack_msg)}")
        else:
            print(f"无法发送确认消息，控制通道状态: {control_channel.readyState if control_channel else 'None'}")

def send_error_message(cmd_type, error):
    """Send error message via appropriate channel"""
    error_msg = {
        "type": "error", 
        "command": cmd_type,
        "error": error
    }
    
    # Try primary data channel first (WebRTC)
    if control_channel and control_channel.readyState == "open":
        try:
            control_channel.send(json.dumps(error_msg))
            print(f"发送错误消息: {json.dumps(error_msg)}")
        except Exception as e:
            print(f"发送错误消息失败: {e}")
    else:
        # If WebRTC is not available, store the error for HTTP polling
        if current_session_id and current_session_id in active_sessions:
            session = active_sessions[current_session_id]
            session.add_command(error_msg)
            print(f"存储错误消息用于HTTP轮询: {json.dumps(error_msg)}")
        else:
            print(f"无法发送错误消息，控制通道状态: {control_channel.readyState if control_channel else 'None'}")
# Add cleanup for inactive sessions
async def cleanup_sessions():
    """定期清理不活动的会话"""
    while True:
        try:
            current_time = datetime.now()
            inactive_sessions = []
            
            for session_id, session in active_sessions.items():
                # Remove sessions inactive for more than 1 hour
                if (current_time - session.last_access).seconds > 3600:
                    inactive_sessions.append(session_id)
            
            for session_id in inactive_sessions:
                del active_sessions[session_id]
                print(f"Cleaned up inactive session: {session_id}")
                
        except Exception as e:
            print(f"Session cleanup error: {e}")
            
        await asyncio.sleep(300)  # Run every 5 minutes

async def on_shutdown(app):
    """应用关闭时清理资源"""
    global pc, control_enabled, inactivity_task
    control_enabled = False

    # Cancel inactivity task
    if inactivity_task:
        inactivity_task.cancel()
    
    # Restore screen and lock session
    restore_screen_and_lock()

    if pc:
        await pc.close()


def debug_sdp(sdp, label="SDP"):
    """Debug SDP content"""
    print(f"[WEBRTC] === {label} DEBUG ===")
    lines = sdp.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('m=') or line.startswith('a=mid:') or 'candidate' in line:
            print(f"[WEBRTC] {label} Line {i}: {line.strip()}")
    print(f"[WEBRTC] === END {label} DEBUG ===")


# @routes.post("/ice-candidate")
async def ice_candidate(request):
    global pc
    print("[WEBRTC] 收到ICE候选者请求")
    # Add this at the beginning of ice_candidate function:
    if pc is None:
        # Wait a bit for pc to be initialized
        for _ in range(10):  # Wait up to 1 second
            await asyncio.sleep(0.1)
            if pc is not None:
                break
        
        if pc is None:
            print("[WEBRTC] PeerConnection未初始化，无法添加候选者")
            return web.Response(status=400, text="连接未初始化")
    # Replace the existing PC check in ice_candidate with:
    if pc is None or pc.signalingState == "closed":
        # Queue the candidate for later processing
        pending_candidates.append(data)
        print(f"[WEBRTC] Queued ICE candidate, PC not ready yet. Queue size: {len(pending_candidates)}")
        return web.Response(text="OK")
    

    try:
        data = await request.json()
        print(f"[WEBRTC] ICE候选者数据: {data}")

        # Add validation for aiortc 1.5.0
        if not data or not isinstance(data, dict):
            return web.Response(status=400, text="Invalid data format")
        
        # 直接使用create_ice_candidate函数解析（简化逻辑）
        candidate = create_ice_candidate(data)
        # Small delay to ensure proper timing in aiortc 1.5.0
        await asyncio.sleep(0.001)
        if not candidate:
            print("[WEBRTC] 无效的候选者格式")
            return web.Response(status=400, text="无效的候选者格式")
        
        if pc is None:
            print("[WEBRTC] PeerConnection未初始化，无法添加候选者")
            return web.Response(status=400, text="连接未初始化")
        
        # Add this more comprehensive check:
        if pc.connectionState in ["closed", "failed"]:
            print("[WEBRTC] PeerConnection状态不正确，无法添加候选者")
            return web.Response(status=400, text="PeerConnection状态不正确")
        
        # For aiortc 1.5.0, accept candidates in most states
        if pc.signalingState == "closed":
            print("[WEBRTC] PeerConnection已关闭，无法添加候选者")
            return web.Response(status=400, text="PeerConnection已关闭")


        # Add after the candidate creation and before adding it:
        print(f"[WEBRTC] Detailed connection state before adding candidate:")
        print(f"  - signalingState: {pc.signalingState}")
        print(f"  - iceConnectionState: {pc.iceConnectionState}")
        print(f"  - iceGatheringState: {pc.iceGatheringState}")
        print(f"  - connectionState: {pc.connectionState}")

        print(f"[WEBRTC] Candidate details:")
        print(f"  - foundation: {getattr(candidate, 'foundation', 'N/A')}")
        print(f"  - component: {getattr(candidate, 'component', 'N/A')}")
        print(f"  - protocol: {getattr(candidate, 'protocol', 'N/A')}")
        print(f"  - priority: {getattr(candidate, 'priority', 'N/A')}")
        print(f"  - ip: {getattr(candidate, 'ip', 'N/A')}")
        print(f"  - port: {getattr(candidate, 'port', 'N/A')}")
        print(f"  - type: {getattr(candidate, 'type', 'N/A')}")
        print(f"  - sdpMid: {getattr(candidate, 'sdpMid', 'N/A')}")
        print(f"  - sdpMLineIndex: {getattr(candidate, 'sdpMLineIndex', 'N/A')}")

        # Before adding the candidate, add a retry mechanism:
        retry_count = 0
        max_retries = 3
        while retry_count < max_retries:
            try:
                print(f"[WEBRTC] 添加ICE候选者: {candidate}")
                await pc.addIceCandidate(candidate)
                print("[WEBRTC] ICE候选者添加成功")
                return web.Response(text="OK")
            except Exception as e:
                retry_count += 1
                if retry_count < max_retries:
                    print(f"[WEBRTC] 添加ICE候选者失败，{retry_count}次尝试后重试: {e}")
                    await asyncio.sleep(0.01 * retry_count)  # Exponential backoff
                else:
                    raise e
        
    except Exception as e:
        print(f"[WEBRTC] 添加ICE候选者失败: {e}")
        import traceback
        traceback.print_exc()
        return web.Response(status=400, text=f"添加失败: {str(e)}")
    

def create_ice_candidate(ice_data):
    """
    从ICE候选者数据创建RTCIceCandidate对象
    
    参数:
        ice_data: 包含candidate、sdpMid和sdpMLineIndex的字典
    返回:
        成功返回RTCIceCandidate对象，失败返回None
    """
    try:
        if not isinstance(ice_data, dict) or 'candidate' not in ice_data:
            return None
        print(f"[WEBRTC] 解析ICE候选者数据: {ice_data}")
        # 解析candidate字符串
        candidate_str = ice_data['candidate']
        if not isinstance(candidate_str, str):
            return None
        print(f"[WEBRTC] 候选者字符串: {candidate_str}")
        # Add more robust parsing for aiortc 1.5.0
        if 'candidate:' not in candidate_str:
            return None
        
        # 更宽松的正则表达式，适配更多候选者格式
        match = re.match(
            r"candidate:(\S+) (\d) (\S+) (\d+) (\S+) (\d+) typ (\S+)(.*)",
            candidate_str
        )
        if not match:
            print(f"[WEBRTC] 无法解析ICE候选者: {candidate_str}")
            return None
            
        # 提取字段（与之前相同）
        foundation = match.group(1)
        component = int(match.group(2))
        protocol = match.group(3)
        priority = int(match.group(4))
        ip = match.group(5)
        port = int(match.group(6))
        type_ = match.group(7)

        # After extracting the type_ field, add:
        # Handle different candidate type representations
        type_mapping = {
            "host": "host",
            "srflx": "srflx",
            "relay": "relay",
            "prflx": "prflx"
        }
        if type_ in type_mapping:
            type_ = type_mapping[type_]
        
        print(f"[WEBRTC] 解析的候选者字段: foundation={foundation}, component={component}, "
              f"protocol={protocol}, priority={priority}, ip={ip}, port={port}, type={type_}")
        
        # After extracting all the fields, add better validation:
        # Validate required fields
        if not foundation or not protocol or not ip or port is None:
            print("[WEBRTC] ICE候选者缺少必要字段")
            return None
            
        # Validate IP address format
        import socket
        try:
            socket.inet_aton(ip)
        except socket.error:
            # If it's not a valid IP, it might be a local candidate name - that's okay for local connections
            print(f"[WEBRTC] 注意: ICE候选者IP可能是本地名称: {ip}")

        # After extracting all fields, add validation for port range:
        if port < 0 or port > 65535:
            print(f"[WEBRTC] ICE候选者端口无效: {port}")
            return None
            
        # Add validation for component (should be 1 for RTP or 2 for RTCP):
        if component not in [1, 2]:
            print(f"[WEBRTC] ICE候选者组件无效: {component}")
            return None
        
        # Add after extracting sdpMid and sdpMLineIndex:
        sdp_mid = ice_data.get('sdpMid')
        sdp_mline_index = ice_data.get('sdpMLineIndex')

        # After parsing the candidate string, add:
        related_address = None
        related_port = None

        # Extract relatedAddress and relatedPort for srflx/relay candidates
        if "raddr" in candidate_str and "rport" in candidate_str:
            raddr_match = re.search(r"raddr (\S+)", candidate_str)
            rport_match = re.search(r"rport (\d+)", candidate_str)
            if raddr_match and rport_match:
                related_address = raddr_match.group(1)
                related_port = int(rport_match.group(1))

        # Before creating RTCIceCandidate, add a check:
        # For host candidates, relatedAddress and relatedPort should be None
        if type_ == "host":
            related_address = None
            related_port = None

        candidate = RTCIceCandidate(
            foundation=foundation,
            component=component,
            protocol=protocol,
            priority=priority,
            ip=ip,
            port=port,
            type=type_,
            relatedAddress=related_address,  # Add this line
            relatedPort=related_port,        # Add this line
            sdpMid=ice_data.get('sdpMid'),
            sdpMLineIndex=ice_data.get('sdpMLineIndex')
        )
        print(f"[WEBRTC] 成功创建RTCIceCandidate对象")
        return candidate
    except Exception as e:
        print(f"[WEBRTC] 创建ICE候选者失败: {e}")
        import traceback
        traceback.print_exc()
        return None

def control_screen(action):
    """Control screen on/off using sysfs interface"""
    try:
        # Path to the display status file
        screen_path = "/sys/class/drm/card0-HDMI-A-1/status"
        
        # Check if the path exists
        if not os.path.exists(screen_path):
            print(f"Screen control path does not exist: {screen_path}")
            return False
            
        # Execute the command
        result = subprocess.run(
            ["sudo", "tee", screen_path],
            input=action,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        if result.returncode == 0:
            print(f"Screen {action} command executed successfully")
            return True
        else:
            print(f"Screen {action} command failed: {result.stderr}")
            return False
            
    except Exception as e:
        print(f"Error executing screen {action} command: {e}")
        return False

def restore_screen_and_lock():
    """Restore screen and lock session"""
    try:
        # Turn screen on
        result = subprocess.run(
            f'echo on | sudo tee /sys/class/drm/card0-HDMI-A-1/status',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        if result.returncode == 0:
            print("屏幕已重新点亮。")
        else:
            print(f"Failed to turn screen on: {result.stderr}")
            
        # Lock session
        result = subprocess.run(
            ['loginctl', 'lock-session'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        if result.returncode == 0:
            print("屏幕已锁定。")
        else:
            print(f"Failed to lock session: {result.stderr}")
            
    except Exception as e:
        print(f"Error in restore_screen_and_lock: {e}")

async def check_inactivity():
    """Check for inactivity and restore screen if timeout reached"""
    global last_activity_time
    while True:
        try:
            if last_activity_time and (time.time() - last_activity_time) > INACTIVITY_TIMEOUT:
                print("Inactivity timeout reached, restoring screen and locking session")
                restore_screen_and_lock()
                # Reset timer to avoid repeated triggering
                last_activity_time = None
                break
            await asyncio.sleep(10)  # Check every 10 seconds
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error in inactivity check: {e}")
            await asyncio.sleep(10)

def signal_handler(signum, frame):
    """Handle termination signals"""
    print(f"Received signal {signum}, restoring screen and locking session")
    restore_screen_and_lock()
    # Exit gracefully
    exit(0)

def lock_session():
    """Lock the current session using loginctl"""
    try:
        result = subprocess.run(
            ['loginctl', 'lock-session'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        if result.returncode == 0:
            print("Session locked successfully")
            return True
        else:
            print(f"Failed to lock session: {result.stderr}")
            return False
            
    except Exception as e:
        print(f"Error locking session: {e}")
        return False

if __name__ == "__main__":
    # Set the DISPLAY
    os.environ['DISPLAY'] = get_active_display()
    print(f"Using DISPLAY: {os.environ['DISPLAY']}")

    # Initialize screen track to get screen dimensions
    screen_track = ScreenShareTrack()
    # After screen_track = ScreenShareTrack() add:
    if hasattr(screen_track, '_stop_event'):
        screen_track._stop_event.clear()

    # Set initial mouse position to center of screen
    if screen_track:
        current_mouse_x = screen_track.original_width // 2
        current_mouse_y = screen_track.original_height // 2
        print(f"初始鼠标位置设置为: ({current_mouse_x}, {current_mouse_y})")
        

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    # 检查xdotool是否安装
    try:
        subprocess.run(["xdotool", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        print("错误: 未找到xdotool。请先安装xdotool: sudo apt install xdotool")
        exit(1)
    
    # 创建Web应用
    app = web.Application()
    app.on_shutdown.append(on_shutdown)

    # Add middleware to log all requests for debugging
    @web.middleware
    async def log_requests(request, handler):
        # print(f"Received {request.method} request for {request.path}")
        response = await handler(request)
        # print(f"Response status for {request.path}: {response.status}")
        return response
    
    app.middlewares.append(log_requests)

    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)
    app.router.add_get("/screen-update", screen_update)
    app.router.add_post("/ice-candidate", ice_candidate)
    
    # Add new routes for alternative communication
    app.router.add_post("/session/create", create_session)
    app.router.add_post("/command/send", send_command_http)
    app.router.add_get("/command/get", get_commands_http)


    # Add startup logic to create background tasks
    async def start_background_tasks(app):
        # Start the HTTP command processing task
        app['http_command_processor'] = asyncio.create_task(process_http_commands())
        
    async def cleanup_background_tasks(app):
        # Clean up background tasks
        if 'http_command_processor' in app:
            app['http_command_processor'].cancel()
            
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    # 获取本机IP地址
    import socket
    def get_local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            IP = s.getsockname()[0]
        except Exception:
            IP = '127.0.0.1'
        finally:
            s.close()
        return IP
    
    local_ip = get_local_ip()
    print(f"被控端服务已启动，主控端可通过浏览器访问 http://{local_ip}:{PORT}")
    
    # 启动Web服务器
    web.run_app(app, host=HOST, port=PORT)

