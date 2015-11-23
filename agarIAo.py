#! /usr/bin/python2.7
# -*- coding: utf-8 -*-

import os
import threading
import urllib2
import websocket
import struct
from time import sleep
import pygame
import pygame.gfxdraw
import sys
from threading import RLock
import math
import matplotlib.pyplot as plt

from neat import population, visualize
from neat.config import Config
from neat.nn import nn_pure as nn

red = (255,0,0)
green = (0,255,0)
blue = (0,0,255)
yellow = (255,255,0)
cian = (0,255,255)
darkBlue = (0,0,128)
white = (255,255,255)
black = (0,0,0)
pink = (255,200,200)
gray = (100,100,100)

featuresColors = [green,red,yellow,cian]

packet_s2c = {
    16: 'world_update',
    17: 'spectate_update',
    20: 'clear_cells',
    21: 'debug_line',
    32: 'own_id',
    49: 'leaderboard_names',
    50: 'leaderboard_groups',
    64: 'world_rect',
    81: 'experience_info',
}

packet_c2s = {
    0: 'respawn',
    1: 'spectate',
    16: 'target',
    17: 'split',
    18: 'spectate_toggle',
    20: 'explode',
    21: 'shoot',
    80: 'token',
    81: 'facebook',
    254: 'handshake1',
    255: 'handshake2',
}

ingame_packets = ('world_rect', 'world_update', 'leaderboard_names',
                  'leaderboard_groups', 'spectate_update', 'own_id')


urlfs = "http://m.agar.io/"
urlinfo = "http://m.agar.io/info"
urlgc = "http://gc.agar.io/"

handshakeVersion = 2200049715

headers = {'User-Agent':'Mozilla/5.0 (Windows NT 6.3; rv:36.0) Gecko/20100101 Firefox/36.0',\
			'Origin': 'http://agar.io','Referer':'http://agar.io'}

class Cell:
    def __init__(self, *args, **kwargs):
        self.pos = ()
        self.update(*args, **kwargs)

    def update(self, cid=-1, x=0, y=0, size=0, name='',
               color=(1, 0, 1), is_virus=False, is_agitated=False): 
        self.cid = cid
        self.pos = (x, y)
        self.size = size
        self.mass = size ** 2 / 100.0
        self.name = getattr(self, 'name', name) or name
        self.color = tuple(map(lambda rgb: rgb / 255.0, color))
        self.is_virus = is_virus
        self.is_agitated = is_agitated
    @property
    def is_food(self):
        return self.size < 20 and not self.name

    @property
    def is_ejected_mass(self):
        return self.size in (37, 38) and not self.name

    def same_player(self, other):
        """
        Compares name and color.
        Returns True if both are owned by the same player.
        """
        return self.name == other.name \
            and self.color == other.color
            

    def __lt__(self, other):
        if self.mass != other.mass:
            return self.mass < other.mass
        return self.cid < other.cid


class World:
	def __init__(self):
		self.cells = {}
		self.cellsMutex = RLock()
		self.cellsMutex.acquire()
		self.cellsMutex.release()
		self.leaderboard_names = []
		self.leaderboard_groups = []
		self.top_left = (0, 0)
		self.bottom_right = (0, 0)
		self.reset()

	def reset(self):
		self.cells.clear()
		del self.leaderboard_names[:]
		del self.leaderboard_groups[:]
		self.top_left = (0, 0)
		self.bottom_right = (0, 0)
		
	def create_cell(self, cid):
		"""
		Creates a new cell in the world.
		Override to use a custom cell class.
		"""
		self.cellsMutex.acquire()
		self.cells[cid] = Cell()
		self.cellsMutex.release()

	@property
	def center(self):
		return (((self.top_left[0] + self.bottom_right[0]) / 2),\
				((self.top_left[1] + self.bottom_right[1]) / 2))

	@property
	def size(self):
		return (abs(self.top_left[0]) + abs(self.bottom_right[0]),\
				abs(self.top_left[1]) + abs(self.bottom_right[1]))
		
		
class Player:
	def __init__(self):
		self.world = World()
		self.own_ids = set()
		self.reset()

	def reset(self):
		self.own_ids.clear()
		self.nick = 'agarIAo'
		self.center = self.world.center
		self.cells_changed()

	def cells_changed(self):
		self.total_size = sum(cell.size for cell in self.own_cells)
		self.total_mass = sum(cell.mass for cell in self.own_cells)
		self.scale = pow(min(1.0, 64.0 / self.total_size), 0.4) \
			if self.total_size > 0 else 1.0
			
		if self.own_ids:
			left = min(cell.pos[0] for cell in self.own_cells)
			right = max(cell.pos[0] for cell in self.own_cells)
			top = min(cell.pos[1] for cell in self.own_cells)
			bottom = max(cell.pos[1] for cell in self.own_cells)
			self.center = ((left + right)/2, (top + bottom)/2)
		# else: keep old center

	@property
	def own_cells(self):
		cells = self.world.cells
		return (cells[cid] for cid in self.own_ids)

	@property
	def is_alive(self):
		return bool(self.own_ids)

	@property
	def is_spectating(self):
		return not self.is_alive

	@property
	def visible_area(self):
		"""
		Calculated like in the official client.
		Returns (top_left, bottom_right).
		"""
		# looks like zeach has a nice big screen
		half_viewport = Vec(1920, 1080) / 2 / self.scale
		top_left = self.world.center - half_viewport
		bottom_right = self.world.center + half_viewport
		return top_left, bottom_right

class BufferUnderflowError(struct.error):
    def __init__(self, fmt, buf):
        self.fmt = fmt
        self.buf = buf
        self.args = ('Buffer too short: wanted %i %s, got %i %s'
                     % (struct.calcsize(fmt), fmt, len(buf), buf),)

class BufferStruct:
    def __init__(self, message):
        self.buffer = message
        self.save = message

    def __str__(self):
        specials = {
            '\r': '\\r',
            '\n': '\\n',
            ' ': '␣',
        }
        nice_bytes = []
        hex_seen = False
        for b in self.buffer:
            if chr(b) in specials:
                if hex_seen:
                    nice_bytes.append(' ')
                    hex_seen = False
                nice_bytes.append(specials[chr(b)])
            elif 33 <= int(b) <= 126:  # printable
                if hex_seen:
                    nice_bytes.append(' ')
                    hex_seen = False
                nice_bytes.append('%c' % b)
            else:
                if not hex_seen:
                    nice_bytes.append(' 0x')
                    hex_seen = True
                nice_bytes.append('%02x' % b)
        return ''.join(nice_bytes)

    def pop_values(self, fmt):
        size = struct.calcsize(fmt)
        if len(self.buffer) < size:
            raise BufferUnderflowError(fmt, self.buffer)
        values = struct.unpack_from(fmt, self.buffer, 0)
        self.buffer = self.buffer[size:]
        return values

    def pop_int8(self):
        return self.pop_values('<b')[0]

    def pop_uint8(self):
        return self.pop_values('<B')[0]

    def pop_int16(self):
        return self.pop_values('<h')[0]

    def pop_uint16(self):
        return self.pop_values('<H')[0]

    def pop_int32(self):
        return self.pop_values('<i')[0]

    def pop_uint32(self):
        return self.pop_values('<I')[0]

    def pop_float32(self):
        return self.pop_values('<f')[0]

    def pop_float64(self):
        return self.pop_values('<d')[0]

    def pop_str16(self):
        l_name = []
        while 1:
            c = self.pop_uint16()
            if (c == 0) or (c > 254) or (c == 14):
                break
            l_name.append(chr(c))
        return ''.join(l_name)

    def pop_str8(self):
        l_name = []
        while 1:
            c = self.pop_uint8()
            if c == 0:
                break
            l_name.append(chr(c))
        return ''.join(l_name)

class agarioClient:
	def __init__(self, gcb = None):
		print("Instanciate agarioClient")
		self.inGame = False
		self.player = Player()
		self.ws = websocket.WebSocket()
		self.running = True
		if gcb:
			self.gameCallback = gcb
		else:
			self.gameCallback = self
			
	#====================
		
	def findServer(self, region = 'EU-London', mode = None):
		print("Find Server")
		if mode:
			region = '%s:%s' % (region, mode)
		data = '%s\n%s' % (region, handshakeVersion)
		req = urllib2.Request(urlfs, data.encode(), headers)
		return urllib2.urlopen(req).read().decode().split("\n")[0:2]
		
	#====================	
		
	def onMessage(self):
		## Receive Msg
		try:
			msg = self.ws.recv()
		except Exception:
			self.disconnect()
			return False
		if not msg:
			self.onError("message","Empty message received")
			return False
			
		## Unpack and parse Msg
		buf = BufferStruct(msg)
		opcode = buf.pop_uint8()
		try:
			packet_name = packet_s2c[opcode]
		except KeyError:
			self.onError("Message","Unknown packet %s" % opcode)
			return False
		if not self.inGame and packet_name in ingame_packets:
			self.inGame = True
		parser = getattr(self, 'parse_%s' % packet_name)
		try:
			parser(buf)
		except:
			print("fuck");
			self.player.world.cellsMutex.release()
			return True
		"""
		except BufferUnderflowError as e:
			m = 'Parsing %s packet failed: %s' % (packet_name, e.args[0])
			self.onError("Message",m)
		if len(buf.buffer) != 0:
			#print(len(buf.buffer))
			m = 'Buffer not empty after parsing "%s" packet (%d)' %(packet_name,len(buf.buffer))
			#self.onError("Message",m)
			#print(":".join("{:02x}".format(ord(c)) for c in msg))
			#self.onError("DUMP",msg)
		"""
		return True
			
	def onError(self, what, msg):
		print("on%sError: %s" %(what, msg))
		
	def onClose(self):
		print("onClose")
				
	#====================					
				
	def connect(self, host, token):
		
		self.address = host
		self.serverToken = token
		self.ws.settimeout(1)
		self.ws.connect( "ws://%s" % host, origin='http://agar.io')
		if not self.ws.connected:
			self.onError("Connection","Could not open ws")
			return False
		
		self.inGame = False	
		self.sendHandshake()
		self.sendToken(self.serverToken)
		
		return True
			
	def listen(self):
		"""Set up a quick connection. Returns on disconnect."""
		import select
		while self.ws.connected:
			if self.running:
				r, w, e = select.select((self.ws.sock, ), (), ())
				if r:
					self.onMessage()
				elif e:
					self.onError("socket","Select Error ... disconnect")
					self.disconnect()
			else:
				self.disconnect()
			
	def disconnect(self):
		self.ws.close()
		self.onClose()
		
	#====================
	
	def parse_world_update(self, buf):
		self.gameCallback.on_world_update_pre()

		# we keep the previous world state, so
		# handlers can print names, check own_ids, ...

		self.player.world.cellsMutex.acquire()
		
		cells = self.player.world.cells

		# ca eats cb
		for i in range(buf.pop_uint16()):
			ca = buf.pop_uint32()
			cb = buf.pop_uint32()
			self.gameCallback.on_cell_eaten(eater_id=ca, eaten_id=cb)
			if cb in self.player.own_ids:  # we got eaten
				if len(self.player.own_ids) <= 1:
					self.gameCallback.on_death()
					# do not clear all cells yet, they still get updated
				self.player.own_ids.remove(cb)
			if cb in cells:
				#print('delete',cb,cells[cb].pos[0],cells[cb].pos[1])
				self.gameCallback.on_cell_removed(cid=cb)
				del cells[cb]

		# create/update cells
		while 1:
			cid = buf.pop_uint32()
			if cid == 0:
				break
			cx = buf.pop_int32()
			cy = buf.pop_int32()
			csize = buf.pop_int16()
			color = (buf.pop_uint8(), buf.pop_uint8(), buf.pop_uint8())

			bitmask = buf.pop_uint8()
			is_virus = bool(bitmask & 1)
			is_agitated = bool(bitmask & 16)
			if bitmask & 2:  # skip padding
				for i in range(buf.pop_uint32()):
					buf.pop_uint8()
			if bitmask & 4:  # skin URL
				#print(":".join("{:02x}".format(ord(c)) for c in buf.save))
				skin_url = buf.pop_str8()
				if skin_url[0] is not ':':
					skin_url = ''
			else:  # no skin URL given
				skin_url = ''

			cname = buf.pop_str16()
			self.gameCallback.on_cell_info(
				cid=cid, x=cx, y=cy, size=csize, name=cname, color=color,
				is_virus=is_virus, is_agitated=is_agitated)
			if cid not in cells:
				self.player.world.create_cell(cid)
			self.player.world.cells[cid].update(
				cid=cid, x=cx, y=cy, size=csize, name=cname, color=color,
				is_virus=is_virus, is_agitated=is_agitated)

		# also keep these non-updated cells
		for i in range(buf.pop_uint32()):
			cid = buf.pop_uint32()
			if cid in cells:
				self.gameCallback.on_cell_removed(cid=cid)
				del cells[cid]
				if cid in self.player.own_ids:  # own cells joined
					self.player.own_ids.remove(cid)

		self.player.cells_changed()

		self.gameCallback.on_world_update_post()
		
		self.player.world.cellsMutex.release()

	def parse_leaderboard_names(self, buf):
		# sent every 500ms
		# not in "teams" mode
		n = buf.pop_uint32()
		leaderboard_names = []
		for i in range(n):
		    l_id = buf.pop_uint32()
		    l_name = buf.pop_str16()
		    leaderboard_names.append((l_id, l_name))
		self.gameCallback.on_leaderboard_names(leaderboard=leaderboard_names)
		self.player.world.leaderboard_names = leaderboard_names

	def parse_leaderboard_groups(self, buf):
		# sent every 500ms
		# only in "teams" mode
		n = buf.pop_uint32()
		leaderboard_groups = []
		for i in range(n):
		    angle = buf.pop_float32()
		    leaderboard_groups.append(angle)
		self.gameCallback.on_leaderboard_groups(angles=leaderboard_groups)
		self.player.world.leaderboard_groups = leaderboard_groups

	def parse_own_id(self, buf):  # new cell ID, respawned or split
		#print("own_id")
		cid = buf.pop_uint32()
		if not self.player.is_alive:  # respawned
		    self.player.own_ids.clear()
		    self.gameCallback.on_respawn()
		# server sends empty name, assumes we set it here
		if cid not in self.player.world.cells:
		    self.player.world.create_cell(cid)
		# self.world.cells[cid].name = self.player.nick
		self.player.own_ids.add(cid)
		self.player.cells_changed()
		self.gameCallback.on_own_id(cid=cid)

	def parse_world_rect(self, buf):  # world size
		left = buf.pop_float64()
		top = buf.pop_float64()
		right = buf.pop_float64()
		bottom = buf.pop_float64()
		self.gameCallback.on_world_rect(
		    left=left, top=top, right=right, bottom=bottom)
		self.player.world.top_left = (top, left)
		self.player.world.bottom_right = (bottom, right)
		self.player.center = self.player.world.center

		if buf.buffer:
		    number = buf.pop_uint32()
		    text = buf.pop_str16()
		    self.gameCallback.on_server_version(number=number, text=text)

	def parse_spectate_update(self, buf):
		# only in spectate mode
		x = buf.pop_float32()
		y = buf.pop_float32()
		scale = buf.pop_float32()
		self.player.center.set(x, y)
		self.player.scale = scale
		self.gameCallback.on_spectate_update(
		    pos=self.player.center, scale=scale)

	def parse_experience_info(self, buf):
		level = buf.pop_uint32()
		current_xp = buf.pop_uint32()
		next_xp = buf.pop_uint32()
		self.gameCallback.on_experience_info(
		    level=level, current_xp=current_xp, next_xp=next_xp)

	def parse_clear_cells(self, buf):
		# TODO clear cells packet is untested
		self.gameCallback.on_clear_cells()
		self.world.cells.clear()
		self.player.own_ids.clear()
		self.player.cells_changed()

	def parse_debug_line(self, buf):
		# TODO debug line packet is untested
		x = buf.pop_int16()
		y = buf.pop_int16()
		self.gameCallback.on_debug_line(x=x, y=y)
		
	#====================			
			
	def sendStruct(self, fmt, *data):
		if self.ws.connected:
			self.ws.send(struct.pack(fmt, *data))
			
	def sendHandshake(self):
		self.sendStruct('<BI', 254, 5)
		self.sendStruct('<BI', 255, handshakeVersion)

	def sendToken(self, token):
		self.sendStruct('<B%iB' % len(token), 80, *map(ord, token))
		self.server_token = token

	def sendFacebook(self, token):
		self.sendStruct('<B%iB' % len(token), 81, *map(ord, token))
		self.facebook_token = token

	def sendRespawn(self):
		nick = self.player.nick
		print(nick)
		self.sendStruct('<B%iH' % len(nick), 0, *map(ord, nick))

	def sendTarget(self, x, y, cid=0):
		self.sendStruct('<BiiI', 16, int(x), int(y), cid)

	def sendSpectate(self):
		self.sendStruct('<B', 1)

	def sendSpectateToggle(self):
		self.sendStruct('<B', 18)

	def sendSplit(self):
		self.sendStruct('<B', 17)

	def sendShoot(self):
		self.sendStruct('<B', 21)

	def sendExplode(self):
		self.sendStruct('<B', 20)
		self.onDeath()
		
class Visualization:
	def __init__(self,player):
		self.screen = pygame.display.set_mode((1900/2,1080/2))
		self.fontSize = 15
		self.myfont = pygame.font.SysFont("monospace", self.fontSize)
		self.player = player
		
	def drawBackGround(self):
		self.screen.fill(gray)
		
	def drawScore(self):
		ps = self.player.total_size
		pm = self.player.total_mass
		
		pygame.draw.rect(self.screen,(128,128,128),((0,1080/2-self.fontSize),(150,self.fontSize)))
		label = self.myfont.render("%d - %d" %(ps,pm), 2, black)
		self.screen.blit(label, (0,1080/2-self.fontSize))
		
	def drawDirection(self,dxdy):
		cCenter = (1900/4,1080/4)
		d = (1900/4+dxdy[0],1080/4+dxdy[1])
		pygame.draw.line(self.screen,white,cCenter,d)
		
	def drawFeatures(self, cells, colors):
		center = self.player.center
		i = 0
		for c in cells:
			for f in c:
				#print(f)
				x = f[1][0]/2 + 1900/4
				y = f[1][1]/2 + 1080/4
				s = f[2]/2
				#print(center)
				#print(x,y,s)
				lines = [(x-s,y-s),(x+s,y-s),(x+s,y+s),(x-s,y+s)]
				pygame.draw.lines(self.screen, colors[i], True, lines, 2 )
			i+=1
		
	def drawCells(self, cells):
		
		for key in cells:
			c = cells[key]
			normColor = tuple(int(255*x) for x in c.color)
			cCenter = (int(c.pos[0]/2-self.player.center[0]/2+1900/4),int(c.pos[1]/2-self.player.center[1]/2+1080/4))
			if(c.size>0):
				if c.is_virus:
					n = 26.0
					angle = (2.0*math.pi)/n
					dSize = 10
					lastXY = (((c.size/2) + dSize/2) * math.cos(0), ((c.size/2) + dSize/2) * math.sin(0))
					lastXY = (lastXY[0]+cCenter[0],lastXY[1]+cCenter[1])
					for i in range(1,int(n+2)):
						newXY = ()
						if (i%2)==0:
							newXY = (\
									((c.size/2) + (dSize/2)) * math.cos(i*angle),\
									((c.size/2) + (dSize/2)) * math.sin(i*angle)\
									)
						else:
							newXY = (\
									((c.size/2) - (dSize/2)) * math.cos(i*angle),\
									((c.size/2) - (dSize/2)) * math.sin(i*angle)\
									)
						newXY = (newXY[0]+cCenter[0],newXY[1]+cCenter[1])
						pygame.draw.line(self.screen,normColor,newXY,lastXY)
						#newXY = (newXY[0]+cCenter[0],newXY[1]+cCenter[1])
						#lastXY = (lastXY[0]+cCenter[0],lastXY[1]+cCenter[1])
						#pygame.draw.line(self.screen, normColor, newXY,lastXY)
						lastXY = newXY
				else:
					pygame.draw.circle(self.screen, normColor, cCenter, c.size/2)
					if not c.is_food and not c.is_ejected_mass:
						label = self.myfont.render(c.name, 2, black)
						self.screen.blit(label, cCenter)
			else:
				pass
				#print("Wrong size?")
		
	def commit(self):
		pygame.display.update()
		
def computeFeatures(player):

	# compute neural network features
	food = []
	em = []
	enemy = []
	virus = []
	center = player.center
	mass = player.total_mass
	if(center[0]) != 0 and (center[1] != 0) and (mass != 0):
		for key in player.world.cells:
			cell = player.world.cells[key]
			dx = 0
			dy = 0
		
			# don't care about absolute value, create a tmp 
			# variable that is shifted.					
			dCenter = (center[0] + 10000, center[1] + 10000)
			dCellPos = (cell.pos[0] + 10000, cell.pos[1] + 10000)
				
			dx = -(dCenter[0]-dCellPos[0])
			dy = -(dCenter[1]-dCellPos[1])	
		
			dxdy = (dx,dy)
			distance = math.sqrt(abs(dx)**2 + abs(dy)**2)
			if cell.is_food:
				food.append((distance,dxdy,cell.size))
			elif cell.is_ejected_mass:
				em.append((distance,dxdy,cell.size))
			elif cell.is_virus:
				virus.append((distance,dxdy,cell.size))
			elif distance != 0:
				enemy.append((distance,dxdy,cell.size))

	# sort data
	food = sorted(food)
	enemy = sorted(enemy)
	virus = sorted(virus)
	em = sorted(em)

	m = 0
	if len(food) != 0:
		if len(food) > 3:
			m = 3
		else:
			m = len(food)
		food = food[:m]
	if len(enemy) != 0:
		if len(enemy) > 3:
			m = 3
		else:
			m = len(enemy)
		enemy = enemy[:m]
	if len(virus) != 0:
		if len(virus) > 3:
			m = 3
		else:
			m = len(virus)
		virus = virus[:m]
	if len(em) != 0:
		if len(em) > 3:
			m = 3
		else:
			m = len(em)
		em = em[:m]
	
	'''
	print("food",food)
	print("enemy",enemy)
	print("virus",virus)
	print("em",em)
	'''

	return [food,enemy,virus,em]

class SubscriberMock(object):
	def __init__(self):
		self.events = []
		self.data = []
		self.v = None
		self.c = None
		self.dead = True
	
	def setAgarIOClient(self,client):
		self.c = client
	
	def setVisualisation(self,vis):
		self.v = vis
	
	def reset(self):
		self.events.clear()
		self.data.clear()
        
	def __getattr__(self, item):
		if str(item) == "on_death":
			print("DEAD")
			self.dead = True
		assert item[:3] == 'on_', str(item)
		assert 'error' not in item, 'Error event emitted'
		event = item[3:]
		data = {}
		self.events.append(event)
		self.data.append(data)
		return lambda **d: data.update(d)

	"""
	def __getattr__(self, item):
		if str(item) == "on_death":
			print("DEAD")
			self.dead = True
		assert item[:3] == 'on_', 'Requested non-event handler from subscriber'
		assert 'error' not in item, 'Error event emitted'
		event = item[3:]
		data = {}
		self.events.append(event)
		self.data.append(data)
		return lambda **d: data.update(d)
	"""
	def evalFitness(self,genomes):
		print("evalFitness")
		for g in genomes:
			net = nn.create_phenotype(g)
			#net = nn.create_fast_feedforward_phenotype(g)
			g.fitness = self.run(net)
			print(g.fitness)
			
			"""
			error = 0.0
			for i, inputs in enumerate(INPUTS):
				# Serial activation propagates the inputs through the entire network.
				output = net.sactivate(inputs)
				error += (output[0] - OUTPUTS[i]) ** 2

			g.fitness = 1 - math.sqrt(error / len(OUTPUTS))
			"""
        
	def run(self, net):
		self.dead = False
		sleep(2);
		self.c.sendRespawn()
		i = 0
		dt = 0.05
		
		self.lifeTime = 0
		self.mass = []
		self.size = []
		self.diffMass = []
		
		self.lastCenter = (0,0)
		malus = 0
		
		while (not self.dead) and (not quit):
		
			self.c.player.world.cellsMutex.acquire()
	
			features = computeFeatures(self.c.player)
			
			self.v.drawBackGround()
			self.v.drawCells(c.player.world.cells)
			self.v.drawFeatures(features,featuresColors)
			self.v.drawScore()
			
			self.v.commit()
			
			self.c.player.world.cellsMutex.release()
			
			self.lifeTime += 1;
			self.mass.append(self.c.player.total_mass)
			self.size.append(self.c.player.total_size)
			if len(self.mass) >= 3:
				if self.mass[-1] != self.mass[-2]:
					self.diffMass.append(self.mass[-1] - self.mass[-2])
					
			inputs = []
			
			for group in features:
				for cell in group:
					inputs.append(cell[0])
					inputs.append(cell[1][0])
					inputs.append(cell[1][1])
					inputs.append(cell[2])
				if len(group) < 3:
					for j in range(4*(3-len(group))):
						if j%4 == 0:
							inputs.append(99999)
						else:
							inputs.append(0)
					
			inputs.append(self.c.player.total_mass)
			
			if self.lastCenter[0] == c.player.center[0]:
				malus += 0.1
			if self.lastCenter[1] == c.player.center[1]:
				malus += 0.1
				
			#print(len(inputs))
			#print(inputs)
			
			output = net.sactivate(inputs)
			print(output)
			
			#Apply neural network output
			self.c.sendTarget(c.player.center[0]+50*(output[0]-0.5),c.player.center[1]+50*(output[1]-0.5))
			if output[2]>0.5:
				self.c.sendSplit()
			if output[3]>0.5:
				self.c.sendShoot()
			
			"""
			if self.c.player.total_mass != 0:
				# Take decision on direction
				x = 30*math.sin(i*dt)+20
	   			y = 30*math.cos(i*dt)
	   			self.c.sendTarget(c.player.center[0]+x,c.player.center[1]+y)
	   			
	   			self.v.drawDirection((x,y))
	   			self.v.commit()
	   		"""
   				
			if i%100 == 0:
				print("alive")
			#print("5")
			i += 1
			sleep(0.01)
		
		"""
		t = range(self.lifeTime)
		plt.plot(t, self.mass)
		plt.show()
		"""
		
		fitness = sum(self.diffMass) - malus
		if fitness < 0:
			fitness = 0
		return fitness
			         
"""
if __name__ == "__main__":
	import threading
	pygame.init()
	c = agarioClient(SubscriberMock())
	v = Visualization(c.player)
	s = c.findServer()
	print(s)
	if c.connect(s[0],s[1]):
	
	else:
		print("Could not connect")
	
"""
if __name__ == "__main__":
	
	# Open config File
	local_dir = os.path.dirname(__file__)
	config = Config(os.path.join(local_dir, 'agarIAo_config'))

	pygame.init()
	p = SubscriberMock()
	c = agarioClient(p)
	v = Visualization(c.player)
	
	p.setAgarIOClient(c)
	p.setVisualisation(v)
	
	quit = False
	
	s = c.findServer()
	print(s)
	if c.connect(s[0],s[1]):
		print("Client connected")
		t1 = threading.Thread(target=c.listen)
		t1.start()

		pop = population.Population(config)
		pop.epoch(p.evalFitness, 200,checkpoint_interval = 1)
		
		"""
		i = 0
		while not quit:
			sleep(2)
			print(i,"iteration")
			i+=1
			f = p.run(None)
			print("fitness for this run  %s" %f)
			for event in pygame.event.get():
				if event.type == pygame.QUIT:
					print("got event quit")
					quit = True
					break
		"""
			
		"""
		quit = False
		while not quit:
			# Quit?
			for event in pygame.event.get():
				if event.type == pygame.QUIT:
					print("got event quit")
					quit = True
					break
			# Keyboard
			pressed = pygame.key.get_pressed()
   			if pressed[pygame.K_r]:
   				print("respawn")
   				c.sendRespawn()
   			if pressed[pygame.K_q]:
   				print("quit")
   				quit = True
		"""
			
			
		c.running = False
		t1.join()
		pygame.quit()
		sys.exit()
	else:
		print("Could not connect")
#"""

