import base64
import os
import traceback
from asyncio import create_task, sleep, Event, Lock
from pathlib import Path
from typing import Optional

import websockets
from fastapi import FastAPI, Form
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedOK
from websockets.legacy.client import WebSocketClientProtocol

from vote import Vote, VoteState

TTV_TOKEN = os.environ['TTV_TOKEN']
TTV_USERNAME = os.environ['TTV_USERNAME']
TTV_CHANNEL = os.environ['TTV_CHANNEL']

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

tmpl = Jinja2Templates(directory="templates")

clips_path = Path('clips.txt')
vote = Vote(clips_path)
vote_event = Event()

socket: Optional[WebSocketClientProtocol] = None
socket_event = Event()
socket_lock = Lock()


@app.on_event('startup')
async def startup():
    create_task(ttv_monitor())


async def ttv_disconnect():
    global socket

    if socket is None:
        return

    socket_event.clear()

    try:
        await socket.close()
    except Exception:
        pass

    socket = None
    print('[TTV] 🔴 Disconnected')


async def ttv_connect():
    global socket

    await ttv_disconnect()

    while True:
        try:
            socket = await websockets.connect('wss://irc-ws.chat.twitch.tv:443')
            break
        except Exception:
            traceback.print_exc()
            await sleep(5)

    socket_event.set()
    print('[TTV] 🟢 Connected')


async def ttv_monitor():
    while True:
        # reset connection if connected
        async with socket_lock:
            if socket_event.is_set():
                await ttv_connect()

        await socket_event.wait()

        print('[TTV] Started monitoring')

        try:
            await socket.send(f'CAP REQ :twitch.tv/membership')
            await socket.send(f'PASS oauth:{TTV_TOKEN}')
            await socket.send(f'NICK {TTV_USERNAME}')
            await socket.send(f'JOIN #{TTV_CHANNEL}')

            while True:
                raw = (await socket.recv()).strip()
                parts = raw.split(' ')

                assert len(parts) >= 2

                if parts[0] == 'PING':
                    nonce = ' '.join(parts[1:])
                    await socket.send(f'PONG {nonce}')
                elif parts[1] == 'PRIVMSG' and parts[2] == f'#{TTV_CHANNEL}':
                    username = parts[0].split('!')[0].lstrip(':')
                    message = ' '.join(parts[3:]).lstrip(':')

                    if message.startswith('!'):
                        message = message.lstrip('!').strip().replace(' ', '').lower()

                        if vote.cast_user_vote(username, message):
                            vote_event.set()
                elif parts[1] in {'JOIN', 'PART'}:
                    pass
                else:
                    print('[TTV]', raw)

        except ConnectionClosedOK:
            pass
        except Exception:
            traceback.print_exc()
            await sleep(5)

        print('[TTV] Stopped monitoring')


@app.get('/')
@app.post('/')
async def index(request: Request):
    if vote.state == VoteState.IDLE:
        return tmpl.TemplateResponse('idle.jinja2', {
            'request': request,
            'vote': vote
        })
    elif vote.state == VoteState.VOTING:
        return tmpl.TemplateResponse('voting.jinja2', {
            'request': request,
            'vote': vote,
            'get_image_url': get_image_url
        })
    elif vote.state == VoteState.RESULTS:
        return tmpl.TemplateResponse('results.jinja2', {
            'request': request,
            'vote': vote,
            'get_image_url': get_image_url
        })

    raise Exception('Not implemented vote state')


@app.post('/cast_vote')
async def cast_vote(rank: str = Form()):
    vote.cast_teo_vote(rank)
    vote.end_clip()

    return RedirectResponse(app.url_path_for(index.__name__))


@app.post('/next_clip')
async def next_clip():
    global vote

    if vote.has_next_clip:
        async with socket_lock:
            if not socket_event.is_set():
                await ttv_connect()

        vote.begin_next_clip()
    else:
        async with socket_lock:
            await ttv_disconnect()

        vote = Vote(clips_path)

    return RedirectResponse(app.url_path_for(index.__name__))


@app.get('/config')
async def get_config(request: Request):
    if vote.state != VoteState.IDLE:
        return RedirectResponse(app.url_path_for(index.__name__))

    with open(clips_path) as f:
        config = f.read()

    return tmpl.TemplateResponse('config.jinja2', {
        'request': request,
        'vote': vote,
        'config': config
    })


@app.post('/config')
async def post_config(config: str = Form()):
    global vote

    if vote.state != VoteState.IDLE:
        return RedirectResponse(app.url_path_for(index.__name__))

    try:
        new_vote = Vote(config)

        assert len(new_vote.clips), 'No clips were loaded'

        with open(clips_path, 'w') as f:
            f.write(config)

    except Exception as e:
        raise HTTPException(500, str(e))

    vote = new_vote

    return RedirectResponse(app.url_path_for(index.__name__))


def get_image_url(rank: str) -> str:
    return app.url_path_for(image.__name__, rank=rank)


@app.get('/img/{rank}')
async def image(rank: str):
    rank_image = next((r.image for r in vote.clip.ranks if r.text == rank), None)

    if rank_image is None:
        raise HTTPException(404)

    if not rank_image.path.exists():
        # https://stackoverflow.com/questions/6018611/smallest-data-uri-image-possible-for-a-transparent-image
        return Response(base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'),
                        media_type='image/gif')

    return FileResponse(rank_image.path)


@app.websocket('/ws')
async def websocket(ws: WebSocket):
    await ws.accept()

    try:
        while True:
            await ws.send_json({'total': vote.total_users_votes()})

            await vote_event.wait()
            vote_event.clear()

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await ws.close(1011, str(e))
