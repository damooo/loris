# -*- coding: utf-8 -*-

from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.routing import Map, Rule
from werkzeug.utils import redirect
from werkzeug.wrappers import Request, Response
from werkzeug.wsgi import SharedDataMiddleware
import ConfigParser
import logging
import logging.config
import os
import struct
import urlparse

# Private Static Constants 
DOT_BMP = ".bmp"
DOT_JPG = ".jpg"
DOT_JPGS = ('.jpg', 'jpeg')
DOT_JP2 = ".jp2"
DOT_JSON = ".json"
DOT_XML = ".xml"

_LIB = os.path.join(os.path.dirname(__file__), 'lib')
_BIN = os.path.join(os.path.dirname(__file__), 'bin')
_ETC = os.path.join(os.path.dirname(__file__), 'etc')
_ENV = {"LD_LIBRARY_PATH":_LIB, "PATH":_LIB + ":$PATH"}

conf_file = os.path.join(_ETC, 'patokah.conf')
logging.config.fileConfig(conf_file)
logr = logging.getLogger('main')
logr.info("Logging initialized")

def create_app():
	app = Patokah()
	return app

# Note when we start to stream big files from the filesystem, see:
# http://stackoverflow.com/questions/5166129/how-do-i-stream-a-file-using-werkzeug

class Patokah(object):
	def __init__(self):
		
		# Configuration - Everything else
		_conf = ConfigParser.RawConfigParser()
		_conf.read(conf_file)

		self.CJPEG = _conf.get('utilities', 'cjpeg')
		self.MKFIFO = _conf.get('utilities', 'mkfifo')
		self.RM = _conf.get('utilities', 'rm')
		
		self.TMP_DIR = _conf.get('directories', 'tmp')
		self.CACHE_ROOT = _conf.get('directories', 'cache_root')
		self.SRC_IMAGES_ROOT = _conf.get('directories', 'src_img_root')
		
		for d in (self.TMP_DIR, self.CACHE_ROOT):
			if not os.path.exists(d):
				os.makedirs(d, 0755)
				logr.info("Created " + d)

		self.url_map = Map([
#			Rule('/', endpoint='???'),
			Rule('/<path:ident>/<region>/<size>/<int:rotation>/<quality><format>', endpoint='get_img'),
			Rule('/<path:id>/info<extension>', endpoint='get_img_metadata'),
			
		])
	
	def dispatch_request(self, request):
		adapter = self.url_map.bind_to_environ(request.environ)
		try:
			endpoint, values = adapter.match()
			return getattr(self, 'on_' + endpoint)(request, **values)
		except NotFound, e:
			return self.error_404(e.description)
		except HTTPException, e:
			return e

	def on_get_img_metadata(self, request, id, extension='.xml'):
		# TODO: all of these MIMEs should be constants, and probably support application/* as well
		mime = None
		resp_code = None
		if extension == '.xml': 
			mime = 'text/xml'
		elif extension == '.json': 
			mime = 'text/json'
		else: # support conneg as well.
			mime = request.accept_mimetypes.best_match(['text/json', 'text/xml'])
			if mime == 'text/json': 
				extension = '.json'
			else: 
				mime = '.xml'
			
		img_path = self._resolve_identifier(id)
		
		# TODO: check for a file to return from the cache first, only if not do
		# we make a new one.
		
		if not os.path.exists(img_path):
			raise NotFound('"' + id + '" does not resolve to an image.')
		
		# check the cache
		cache_dir = os.path.join(self.CACHE_ROOT, id)
		if not os.path.exists(cache_dir):
			os.makedirs(cache_dir, 0755)
			logr.debug('made ' + cache_dir)
		cache_path = os.path.join(cache_dir, 'info') + extension
		
		if os.path.exists(cache_path):
			resp = file(cache_path)
		else:
			info = ImgInfo.fromJP2(img_path)
			info.id = id
			if mime == 'text/xml':
				resp = info.toXML()
			else:
				resp = info.toJSON()
			
			# we could fork this off...
			f = open(cache_path, 'w')
			f.write(resp)
			f.close()
			logr.info('Created: ' + cache_path)
			
		return Response(resp, mimetype=mime,)
	
	# Do we want: http://docs.python.org/library/queue.html ?


	def on_get_img(self, request, ident, region='full', size='full', rotation=0, quality='native', format='.jpg'):
		return Response(ident + ' ' + region + ' ' + size + ' ' + str(rotation) + ' ' + quality + ' ' + format)

	def x_on_get_img(self, ident, region='full', size='full', rotation=0, quality='native', format='.jpg'):
		"""
		@return the path to the new image.
		"""
		jp2 = resolve_identifier(id)
		rotation = str(90 * int(rotation / 90)) # round to closest factor of 90
		
		out_dir = os.path.join(self.CACHE_ROOT, id, region, size, rotation)
		out = os.path.join(out_dir, quality) + format  
		
		# Use a named pipe to give kdu and cjpeg format info.
		fifopath = os.path.join(self.TMP_DIR, rand_str() + _BMP)
		mkfifo_cmd = self.MKFIFO + " " + fifopath
		logr.debug(mkfifo_cmd) 
		mkfifo_proc = subprocess.Popen(mkfifo_cmd, shell=True)
		mkfifo_proc.wait()
		
		# Build the kdu_expand call
		kdu_cmd = KDU_EXPAND + " -i " + jp2 
		if region != 'full': kdu_cmd = kdu_cmd + " -region " + region
		if rotation != 0:  kdu_cmd = kdu_cmd + " -rotate " + rotation
		kdu_cmd = kdu_cmd + " -o " + fifopath
		logr.debug(kdu_cmd)
		kdu_proc = subprocess.Popen(kdu_cmd, env=_ENV, shell=True)
	
		# What are the implications of not being able to wait here (not sure why
		# we can't, but it hangs when we try). I *think* that as long as there's 
		# data flowing into the pipe when the next process (below) starts we're 
		# just fine.
		
		# TODO: if format is not jpg, [do something] (see spec)
		# TODO: quality, probably in the recipe below
		
		if not os.path.exists(out_dir):
			os.makedirs(out_dir, 0755)
			self.logr.info("Made directory: " + out_dir)
		cjpeg_cmd = self.CJPEG + " -outfile " + out + " " + fifopath 
		logr.debug(cjpeg_cmd)
		cjpeg_proc = subprocess.call(cjpeg_cmd, shell=True)
		self.logr.info("Made file: " + out)
	
		rm_cmd = self.RM + " " + fifopath
		logr.debug(rm_cmd)
		rm_proc = subprocess.Popen(rm_cmd, shell=True)
		
		return out

	# static?
	def _resolve_identifier(self, ident):
		"""
		Given the identifier of an image, resolve it to an actual path. This
		would need to be overridden to suit different environments.
		
		This simple version just prepends a constant path to the identfier
		supplied, and appends a file extension, resulting in an absolute path 
		on the filesystem.
		"""
		return os.path.join(self.SRC_IMAGES_ROOT, ident + DOT_JP2)

	#TODO: http://library.stanford.edu/iiif/image-api/#errors
	def error_404(self, message):
		response = Response(message, mimetype='text/plain')
		response.status_code = 404
		return response

	def wsgi_app(self, environ, start_response):
		request = Request(environ)
		response = self.dispatch_request(request)
		return response(environ, start_response)

	def __call__(self, environ, start_response):
		return self.wsgi_app(environ, start_response)


class ImgInfo(object):
	# TODO: look at color info in the file and figure out qualities
	def __init__(self):
		self.id = None
		self.width = None
		self.height = None
		self.tile_width = None
		self.tile_height = None
		self.levels = None
	
	@staticmethod
	def fromJP2(path):
		info = ImgInfo()
		"""
		Get the dimensions and levels of a JP2. There's enough going on here;
		make sure the file is available (exists and readable) before passing it.
		
		@see:  http://library.stanford.edu/iiif/image-api/#info
		"""
		jp2 = open(path, 'rb')
		jp2.read(2)
		b = jp2.read(1)
		
		while (ord(b) != 0xFF):	b = jp2.read(1)
		b = jp2.read(1) #skip over the SOC, 0x4F 
		
		while (ord(b) != 0xFF):	b = jp2.read(1)
		b = jp2.read(1) # 0x51: The SIZ marker segment
		if (ord(b) == 0x51):
			jp2.read(4) # get through Lsiz, Rsiz (16 bits each)
			info.width = int(struct.unpack(">HH", jp2.read(4))[1]) # Xsiz (32)
			info.height = int(struct.unpack(">HH", jp2.read(4))[1]) # Ysiz (32)
			logr.debug(path + " w: " + str(info.width))
			logr.debug(path + " h: " + str(info.height))
			jp2.read(8) # get through XOsiz , YOsiz  (32 bits each)
			info.tile_width = int(struct.unpack(">HH", jp2.read(4))[1]) # XTsiz (32)
			info.tile_height = int(struct.unpack(">HH", jp2.read(4))[1]) # YTsiz (32)
			logr.debug(path + " tw: " + str(info.tile_width))
			logr.debug(path + " th: " + str(info.tile_height))

		while (ord(b) != 0xFF):	b = jp2.read(1)
		b = jp2.read(1) # 0x52: The COD marker segment
		if (ord(b) == 0x52):
			jp2.read(7) # through Lcod, Scod, SGcod (16 + 8 + 32 = 56 bits)
			info.levels = int(struct.unpack(">B", jp2.read(1))[0])
			logr.debug(path + " l: " + str(info.levels)) 
		jp2.close()
			
		return info
	
	def toXML(self):
		# cheap!
		x = '<?xml version="1.0" encoding="UTF-8"?>' + os.linesep
		x = x + '<info xmlns="http://library.stanford.edu/iiif/image-api/ns/">' + os.linesep
		x = x + '  <identifier>' + self.id + '</identifier>' + os.linesep
		x = x + '  <width>' + str(self.width) + '</width>' + os.linesep
		x = x + '  <height>' + str(self.height) + '</height>' + os.linesep
		x = x + '  <scale_factors>' + os.linesep
		for s in range(1, self.levels):
			x = x + '    <scale_factor>' + str(s) + '</scale_factor>' + os.linesep
		x = x + '  </scale_factors>' + os.linesep
		x = x + '  <tile_width>' + str(self.tile_width) + '</tile_width>' + os.linesep
		x = x + '  <tile_height>' + str(self.tile_height) + '</tile_height>' + os.linesep
		x = x + '  <formats>' + os.linesep
		x = x + '    <format>jpg</format>' + os.linesep
		x = x + '  </formats>' + os.linesep
  		x = x + '  <qualities>' + os.linesep
  		x = x + '    <quality>native</quality>' + os.linesep
  		x = x + '  </qualities>' + os.linesep
  		x = x + '</info>' + os.linesep
		return x
	
	def toJSON(self):
		# cheaper!
		j = '{' + os.linesep
		j = j + '  "identifier" : "' + self.id + '",' + os.linesep
		j = j + '  "width" : ' + str(self.width) + ',' + os.linesep
		j = j + '  "height" : ' + str(self.height) + ',' + os.linesep
		j = j + '  "scale_factors" : [' + ", ".join(str(l) for l in range(1, self.levels)) + '],' + os.linesep
		j = j + '  "tile_width" : ' + str(self.tile_width) + ',' + os.linesep
		j = j + '  "tile_height" : ' + str(self.tile_height) + ',' + os.linesep
		j = j + '  "formats" : [ "jpg" ],' + os.linesep
		j = j + '  "quality" : [ "native" ]' + os.linesep
		j = j + '}'
		return j

if __name__ == '__main__':
    from werkzeug.serving import run_simple
    app = create_app()
    run_simple('127.0.0.1', 5000, app, use_debugger=True, use_reloader=True)

