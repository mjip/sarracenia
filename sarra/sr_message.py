#!/usr/bin/env python3
#
# This file is part of sarracenia.
# The sarracenia suite is Free and is proudly provided by the Government of Canada
# Copyright (C) Her Majesty The Queen in Right of Canada, Environment Canada, 2008-2015
#
# Questions or bugs report: dps-client@ec.gc.ca
# sarracenia repository: git://git.code.sf.net/p/metpx/git
# Documentation: http://metpx.sourceforge.net/#SarraDocumentation
#
# sr_message.py : python3 utility tools for sarracenia amqp message processing
#
#
# Code contributed by:
#  Michel Grenier - Shared Services Canada
#  Last Changed   : Sep 22 10:41:32 EDT 2015
#  Last Revision  : Sep 22 10:41:32 EDT 2015
#
########################################################################
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful, 
#  but WITHOUT ANY WARRANTY; without even the implied warranty of 
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the 
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307  USA
#
#

import calendar,os,socket,sys,time,urllib,urllib.parse

# AMQP limits headers to 'short string', or 255 characters, so truncate and warn.
amqp_ss_maxlen = 253


try :
         from sr_util         import *
except :
         from sarra.sr_util    import *

class sr_message():

    def __init__(self,parent):
        self.parent        = parent
        self.logger        = parent.logger

        self.bufsize       = parent.bufsize

        self.message_ttl   = 0
        self.exchange      = None
        self.report_exchange  = parent.report_exchange
        self.report_publisher = None
        self.publisher     = None
        self.pub_exchange  = None
        self.topic         = None
        self.notice        = None
        self.headers       = {}

        self.partstr       = None
        self.sumstr        = None
        self.sumflg        = None

        self.part_ext      = 'Part'

        self.sumalgo       = parent.sumalgo

        self.inplace       = True

        self.user          = None

        self.host          = socket.getfqdn()

        self.add_headers   = self.parent.headers_to_add
        self.del_headers   = self.parent.headers_to_del

        self.isPulse       = False
        self.isRetry       = False

        # important working attributes set to None at startup
        
        self.baseurl       = None
        self.relpath       = None
        self.new_dir       = None
        self.new_file      = None
        self.new_baseurl   = None
        self.new_relpath   = None


    def change_partflg(self, partflg ):
        self.partflg       =  partflg 
        self.partstr       = '%s,%d,%d,%d,%d' %\
                             (partflg,self.chunksize,self.block_count,self.remainder,self.current_block)

    def content_should_not_be_downloaded(self):
        """
        if the file advertised is newer than the local one, and it has a different checksum, return True.

        """
        fname = "%s%s%s" %  (self.new_dir, '/', self.new_file )
        self.logger.debug("sr_message content_match %s" % (fname ) )
        
        self.local_checksum = None

        if not os.path.isfile(fname) : return False

        # insert : file big enough to compute part checksum ?

        lstat = os.stat(fname)
        fsiz  = lstat[stat.ST_SIZE] 
        end   = self.local_offset + self.length

        # compare sizes... if (sr_subscribe is downloading partitions into taget file) and (target_file isn't fully done)
        # This check prevents random halting of subscriber (inplace on) if the messages come in non-sequential order
        if (self.target_file == self.new_file) and (fsiz != self.filesize):
          return False

        # compare dates...

        if self.parent.preserve_time and 'mtime' in self.headers:
            new_mtime = timestr2flt(self.headers[ 'mtime' ])
            if new_mtime <= lstat[stat.ST_MTIME]:
               return True

        if self.sumflg in ['0','n','z'] : 
            return False
 
        if end > fsiz :
           self.logger.debug("sr_message content_match file not big enough... considered different")
           return False

        try   : self.compute_local_checksum()
        except: 
                self.logger.debug("sr_message something went wrong when computing local checksum... considered different")
                return False

        return self.local_checksum == self.checksum

    def compute_local_checksum(self):
        self.logger.debug("sr_message compute_local_checksum new_dir=%s, new_file=%s" % ( self.new_dir, self.new_file ) )

        bufsize = self.bufsize
        if self.length < bufsize : bufsize = self.length

        self.sumalgo.set_path(os.path.basename(self.new_file))

        fp = open( self.new_dir + '/' + self.new_file,'rb')
        if self.local_offset != 0 : fp.seek(self.local_offset,0)
        i  = 0
        while i<self.length :
              buf = fp.read(bufsize)
              if not buf: break
              self.sumalgo.update(buf)
              i  += len(buf)
        fp.close()

        if i != self.length :
           self.logger.warning("sr_message compute_local_checksum incomplete reading %d %d" % (i,self.length))
           self.local_checksum = '0'
           return

        self.local_checksum = self.sumalgo.get_value()


    def from_amqplib(self, msg=None ):

        self.start_timer()

        #self.logger.debug("attributes= %s" % vars(msg))
        if msg :
           self.exchange  = msg.delivery_info['exchange']
           self.topic     = msg.delivery_info['routing_key']
           self.topic     = self.topic.replace('%20',' ')
           self.topic     = self.topic.replace('%23','#')
           self.headers   = msg.properties['application_headers']
           self.notice    = msg.body
           self.isRetry   = msg.isRetry

           if type(self.notice) == bytes: self.notice = self.notice.decode("utf-8")

        # pulse message... parse it
        # exchange='v02.pulse'
        # notice='epoch_time.msec a_pulse message to log'

        self.isPulse = False
        if self.topic.startswith('v02.pulse'):
           self.urlstr  = None
           self.isPulse = True

           # parse pulse notice
           token     = self.notice.split(' ')
           self.time = token[0]
           self.set_msg_time()

           # pulse message
           if self.topic == 'v02.pulse.message':
              pulse_message = ' '.join(token[1:])
              self.logger.warning("pulse message = %s" % pulse_message)
              return

           return

        # retransmission case :
        # topic is name of the queue...
        # set exchange to xpublic
        # rebuild topic from notice : v02.post....

        if self.exchange == '' and self.topic[:2] == 'q_':
           self.logger.debug(" retransmit topic = %s" % self.topic)
           token = self.notice.split(' ')
           self.exchange = 'xpublic'
           if hasattr(self.headers,'exchange') :
              self.exchange = self.headers['exchange']
              del self.headers['exchange']

           path  = token[2].strip('/')
           words = path.split('/')
           self.topic    = 'v02.post.' + '.'.join(words[:-1])
           self.logger.debug(" modified for topic = %s" % self.topic)

        # adjust headers from -headers option

        self.trim_headers()

        self.partstr     = None
        self.sumstr      = None

        token        = self.topic.split('.')
        self.version = token[0]

        if self.version == 'v00' :
           self.parse_v00_post()

        if self.version == 'v02' :
           self.parse_v02_post()

    def get_elapse(self):
        return time.time()-self.tbegin

    def report_publish(self,code,message):
        self.code               = code
        self.headers['message'] = message
        self.report_topic          = self.topic.replace('.post.','.report.')
        self.report_notice         = "%s %d %s %s %f" % \
                                  (self.notice,self.code,self.host,self.user,self.get_elapse())
        self.set_hdrstr()

        # AMQP limits topic to 255 characters, so truncate and warn.
        if len(self.topic.encode("utf8")) >= amqp_ss_maxlen :
           mxlen=amqp_ss_maxlen 
           # see truncation explanation from above.
           while( self.report_topic.encode("utf8")[mxlen-1] & 0xc0 == 0xc0 ):
               mxlen -= 1

           self.report_topic = self.report_topic.encode("utf8")[0:mxlen].decode("utf8")
           self.logger.warning( "truncating reporting topic at %d characters (to fit 255 byte AMQP limit) to: %s " % \
                        ( len(self.report_topic) , self.report_topic ) )


        # if  there is a publisher

        if self.report_publisher != None :

           # run on_report plugins
           for plugin in self.parent.on_report_list :
               if not plugin(self.parent): return False

           # publish
           self.report_publisher.publish(self.report_exchange,self.report_topic,self.report_notice,self.headers)

        self.logger.debug("%d %s : %s %s %s" % (code,message,self.report_topic,self.report_notice,self.hdrstr))

        # make sure not published again
        del self.headers['message']

    def parse_v00_post(self):
        token             = self.topic.split('.')
        # v00             = token[0]
        # dd              = token[1]
        # notify          = token[2]
        self.version      = 'v02'
        self.mtype        = 'post'
        self.topic_prefix = 'v02.post'
        self.subtopic     = '.'.join(token[3:])
        self.topic        = self.topic_prefix + '.' + self.subtopic

        token        = self.notice.split(' ')
        self.baseurl = token[2]
        self.relpath = token[3].replace(    '%20',' ')
        self.relpath = self.relpath.replace('%23','#')

        self.set_notice(token[2],token[3])

        url          = urllib.parse.urlparse(token[2]+token[3])
        
        self.checksum = token[0]
        self.filesize = int(token[1])

        self.headers['source'] = 'metpx'

        self.partstr = '1,%d,1,0,0' % self.filesize
        self.headers['parts'] = self.partstr

        self.sumstr  = 'd,%s' % self.checksum
        self.headers['sum'] = self.sumstr

        self.to_clusters = []
        self.headers['to_clusters'] = None

        self.suffix  = ''
        
        self.set_parts_str(self.partstr)
        self.set_sum_str(self.sumstr)
        self.set_suffix()
        self.set_hdrstr()

    def parse_v02_post(self):

        token         = self.topic.split('.')
        self.version  = token[0]
        self.mtype    = token[1]
        self.topic_prefix = '.'.join(token[:2])
        self.subtopic     = '.'.join(token[3:])

        token        = self.notice.split(' ')
        self.time    = token[0]
        self.baseurl = token[1]
        self.relpath = token[2].replace(    '%20',' ')
        self.relpath = self.relpath.replace('%23','#')
        self.urlstr  = token[1]+token[2]
        self.url     = urllib.parse.urlparse(self.urlstr)

        if self.mtype == 'report' or self.mtype == 'log': # log included for compatibility... prior to rename..
           self.report_code   = int(token[3])
           self.report_host   = token[4]
           self.report_user   = token[5]
           self.report_elapse = float(token[6])

        self.partstr = None
        if 'parts'   in self.headers :
           self.partstr  = self.headers['parts']

        self.sumstr  = None
        if 'sum'     in self.headers :
           self.sumstr   = self.headers['sum']

        self.to_clusters = []
        if 'to_clusters' in self.headers :
           self.to_clusters  = self.headers['to_clusters'].split(',')

        self.suffix = ''

        self.set_parts_str(self.partstr)
        self.set_sum_str(self.sumstr)
        self.set_suffix()
        self.set_msg_time()
        self.set_hdrstr()

    def part_suffix(self):
        return '.%d.%d.%d.%d.%s.%s' %\
               (self.chunksize,self.block_count,self.remainder,self.current_block,self.sumflg,self.part_ext)

    def publish(self):
        ok = False

        if self.pub_exchange != None : self.exchange = self.pub_exchange

        for h in self.headers:
           if len(self.headers[h].encode("utf8")) >= amqp_ss_maxlen:

                # strings in utf, and if names have special characters, the length
                # of the encoded string wll be longer than what is returned by len(. so actually need to look
                # at the encoded length ...  len ( self.headers[h].encode("utf-8") ) < 255
                # but then how to truncate properly. need to avoid invalid encodings.
                mxlen=amqp_ss_maxlen
                while( self.headers[h].encode("utf8")[mxlen-1] & 0xc0 == 0xc0 ):
                      mxlen -= 1

                self.headers[h] = self.headers[h].encode("utf8")[0:mxlen].decode("utf8")
                self.logger.warning( "truncating %s header at %d characters (to fit 255 byte AMQP limit) to: %s " % \
                        ( h, len(self.headers[h]) , self.headers[h]) )

        # AMQP limits topic to 255 characters, space and # replaced, if greater than limit : truncate and warn.
        self.topic = self.topic.replace(' ','%20')
        self.topic = self.topic.replace('#','%23')
        if len(self.topic.encode("utf8")) >= amqp_ss_maxlen :
           mxlen=amqp_ss_maxlen 
           # see truncation explanation from above.
           while( self.topic.encode("utf8")[mxlen-1] & 0xc0 == 0xc0 ):
               mxlen -= 1

           self.topic = self.topic.encode("utf8")[0:mxlen].decode("utf8")
           self.logger.warning( "truncating topic at %d characters (to fit 255 byte AMQP limit) to: %s " % \
                        ( len(self.topic) , self.topic ) )
        

        # in order to split winnowing into multiple instances, directs items with same checksum
        # to same shard. do that by keying on the last character of the checksum.
        # 
        if self.post_exchange_split > 0 :
           suffix= "%02d" % ( ord(self.sumstr[-1]) % self.post_exchange_split )
           self.logger.debug( "post_exchange_split set, keying on %s , suffix is %s" % ( self.sumstr[-1], suffix) )
        else:
           suffix=""

        if self.publisher != None :
           ok = self.publisher.publish(self.exchange+suffix,self.topic,self.notice,self.headers,self.message_ttl)

        self.set_hdrstr()

        if ok :
                self.logger.debug("Published1: %s %s" % (self.exchange,self.topic))
                self.logger.debug("Published2: '%s' %s" % (self.notice, self.hdrstr))
        else  :
                self.printlog = self.logger.error
                self.printlog("Could not publish message :")

                self.printlog("exchange %s topic %s " % (self.exchange,self.topic))
                self.printlog("notice   %s"           % self.notice )
                self.printlog("headers  %s"           % self.hdrstr )

        return ok

    def set_exchange(self,name):
        self.exchange = name

    def set_file(self, new_file, sumstr):
        """ 
            set_file: modify a message to reflect a new file.
                      make a file URL of the new_file.
            sumstr should be the properly formatted checksum field for a message
              '<algorithm>,<value>', e.g.  'd,cbe9047d1b979561bed9a334111878c6'
            to be used by filter plugins when changing the output url.
        """
        fstat = os.stat(new_file)

        # Modify message for posting.

        self.baseurl = 'file:'
        self.relpath = new_file

        self.urlstr = 'file:/' + new_file
        self.url = urllib.parse.urlparse(self.urlstr)

        path  = new_file.strip('/')
        words = path.split('/')
        self.topic = 'v02.post.' + '.'.join(words[:-1])

        self.headers[ 'sum' ] = sumstr
        self.headers[ 'parts' ] = '1,%d,0,0' % fstat.st_size
        self.headers[ 'mtime' ] = timeflt2str(fstat.st_mtime)

        self.set_notice(self.baseurl,self.relpath)

    def set_hdrstr(self):
        self.hdrstr  = ''

        for h in sorted(self.headers):
           self.hdrstr += '%s=%s ' % (h, self.headers[h])

        # added for v00 compatibility (old version of dd_subscribe)
        # can be taken off when v02 will be fully deployed and end user uses sr_subscribe
        #self.headers['filename'] = os.path.basename(self.relpath).split(':')[0][0:200]


    # Once we know the local file we want to use
    # we can have a few flavor of it

    def set_new(self):

        self.local_offset  = 0
        self.in_partfile   = False
        self.local_checksum= None
       
        self.inplace       = self.parent.inplace
        self.target_file   = None

        # file to file

        if self.partflg == '1' : return
        if self.partflg == None : return
        if (self.partflg != 'i') and ( self.partflg != 'p' ):
            self.logger.error("sr_message/set_new message received with invalid partflg=%s" % (self.partflg) )
            return
            
     
        # part file never inserted

        if not self.inplace :

           self.in_partfile = True

           # part file to part file

           if self.partflg == 'p' : return

           # file inserts to part file

           if self.partflg == 'i' :
              self.new_file    += self.suffix
              self.new_relpath += self.suffix
              return

        
        # part file inserted

        if self.inplace :

           # part file inserts to file (maybe in file, maybe in part file)

           if self.partflg == 'p' :
              self.target_file    = self.new_file.replace(self.suffix,'')
              self.target_relpath = self.new_relpath.replace(self.suffix,'')
              part_file    = self.new_file
              part_relpath = self.new_relpath
        
           # file insert inserts into file (maybe in file, maybe in part file)

           elif self.partflg == 'i' :
              self.target_file    = self.new_file
              self.target_relpath = self.new_relpath
              part_file           = self.new_file + self.suffix
              part_relpath        = self.new_relpath + self.suffix

           # default setting : redirect to temporary part file

           self.new_file    = part_file
           self.new_relpath = part_relpath
           self.in_partfile = True
        
           # try to make this message a file insert

           # file exists
           self.target_path = self.new_dir + '/' + self.target_file
           if os.path.isfile(self.target_path) :
              self.logger.debug("new_file exists")
              lstat   = os.stat(self.target_path)
              fsiz    = lstat[stat.ST_SIZE] 

              self.logger.debug("offset vs fsiz %d %d" % (self.offset,fsiz ))
              # part/insert can be inserted 
              if self.offset <= fsiz :
                 self.logger.debug("insert")
                 self.new_file     = self.target_file
                 self.new_relpath  = self.target_relpath
                 self.local_offset = self.offset
                 self.in_partfile  = False
                 return

              # in temporary part file
              self.logger.debug("exist but no insert")
              return


           # file does not exists but first part/insert ... write directly to new_file
           elif self.current_block == 0 :
              self.logger.debug("not exist but first block")
              self.new_file    = self.target_file
              self.new_relpath = self.target_relpath
              self.in_partfile = False
              return

           # file does not exists any other part/insert ... put in temporary part_file
           else :
              self.logger.debug("not exist and not first block")
              self.in_partfile = True
              return
                 
        # unknow conditions

        self.logger.error("bad unknown conditions")
        return

    def set_msg_time(self):
        parts       = self.time.split('.')
        ts          = time.strptime(parts[0]+" +0000", "%Y%m%d%H%M%S %z" )
        ep_msg      = calendar.timegm(ts)
        self.tbegin = ep_msg + float('0.'+parts[1])

    def set_notice_url(self,url,time=None):
        self.url    = url
        self.time   = time
        if time    == None : self.set_time()
        path        = url.path.strip('/')
        notice_path = path.replace(       ' ','%20')
        notice_path = notice_path.replace('#','%23')

        if url.scheme == 'file' :
           self.notice = '%s %s %s' % (self.time,'file:','/'+notice_path)
           return

        urlstr      = url.geturl()
        static_part = urlstr.replace(url.path,'') + '/'

        if url.scheme == 'http' :
           self.notice = '%s %s %s' % (self.time,static_part,notice_path)
           return

        if url.scheme[-3:] == 'ftp'  :
           if url.path[:2] == '//'   : notice_path = '/' + notice_path

        self.notice = '%s %s %s' % (self.time,static_part,notice_path)

    def set_notice(self,baseurl,relpath,time=None):

        self.time    = time
        self.baseurl = baseurl
        self.relpath = relpath
        if not time  : self.set_time()

        notice_relpath = relpath.replace(       ' ','%20')
        notice_relpath = notice_relpath.replace('#','%23')

        self.notice = '%s %s %s' % (self.time,baseurl,notice_relpath)

        #========================================
        # COMPATIBILITY TRICK  for the moment

        self.urlstr  = baseurl+notice_relpath
        self.url     = urllib.parse.urlparse(self.urlstr)
        #========================================


    def set_parts(self,partflg='1',chunksize=0, block_count=1, remainder=0, current_block=0):
        self.partflg          = partflg 
        self.chunksize        = chunksize
        self.block_count      = block_count
        self.remainder        = remainder
        self.current_block    = current_block
        self.partstr          = '%s,%d,%d,%d,%d' %\
                                (partflg,chunksize,block_count,remainder,current_block)
        self.lastchunk        = current_block == block_count-1
        self.headers['parts'] = self.partstr

        self.offset        = self.current_block * self.chunksize
        self.filesize      = self.block_count * self.chunksize
        if self.remainder  > 0 :
           self.filesize  += self.remainder   - self.chunksize
           if self.lastchunk : self.length    = self.remainder

    def set_parts_str(self,partstr):

        self.partflg = None
        self.partstr = partstr
        self.length  = 0

        if self.partstr == None : return

        token        = self.partstr.split(',')
        self.partflg = token[0]

        self.chunksize     = int(token[1])
        self.block_count   = 1
        self.remainder     = 0
        self.current_block = 0
        self.lastchunk     = True

        self.offset        = 0
        self.length        = self.chunksize

        self.filesize      = self.chunksize

        if self.partflg in [ '0', '1' ]: return

        self.block_count   = int(token[2])
        self.remainder     = int(token[3])
        self.current_block = int(token[4])
        self.lastchunk     = self.current_block == self.block_count-1

        self.offset        = self.current_block * self.chunksize

        self.filesize      = self.block_count * self.chunksize

        if self.remainder  > 0 :
           self.filesize  += self.remainder   - self.chunksize
           if self.lastchunk : self.length    = self.remainder

    def set_rename(self,rename=None):
        if rename != None :
           self.headers['rename'] = rename
        elif 'rename' in self.headers :
           del self.headers['rename']

    def set_source(self,source=None):
        if source != None :
           self.headers['source'] = source
        elif 'source' in self.headers :
           del self.headers['source']

    def set_sum(self,sumflg='d',checksum=0):
        self.sumflg   =  sumflg
        self.checksum =  checksum
        self.sumstr   = '%s,%s' % (sumflg,checksum)
        self.headers['sum'] = self.sumstr

    def set_sum_str(self,sumstr):
        self.sumflg  = None
        self.sumalgo = None
        self.sumstr  = sumstr
        if sumstr == None : return

        token        = self.sumstr.split(',')
        self.sumflg  = token[0]
        self.checksum= token[1]

        # file to be removed
        if self.sumflg == 'R' : return

        # keep complete z description in sumflg
        if self.sumflg == 'z':
           self.sumflg  = sumstr

        self.parent.set_sumalgo(self.sumflg)
        self.sumalgo = self.parent.sumalgo

    def set_suffix(self):
        if self.partstr == None : return
        if self.sumstr  == None or self.sumflg == 'R' : return
        self.suffix = self.part_suffix()

    def set_time(self):
        now  = time.time()
        frac = '%f' % now
        nows = time.strftime("%Y%m%d%H%M%S",time.gmtime()) + '.' + frac.split('.')[1]
        self.time = nows
        if not hasattr(self,'tbegin') : self.tbegin = now

    def set_to_clusters(self,to_clusters=None):
        if to_clusters != None :
           self.headers['to_clusters'] = to_clusters
           self.to_clusters = to_clusters.split(',')
        elif 'to_clusters' in self.headers :
           del self.headers['to_clusters']
           self.to_clusters = []

    def set_topic(self,topic_prefix,relpath):
        self.topic_prefix = topic_prefix
        self.topic        = topic_prefix
        self.subtopic     = ''

        strpath           = relpath.strip('/')
        words             = strpath.split('/')
        if len(words) > 1 :
           self.subtopic = '.'.join(words[:-1])
           self.topic   += '.' + self.subtopic

        self.topic       = self.topic.replace('..','.')

    def set_topic_url(self,topic_prefix,url):
        self.topic_prefix = topic_prefix
        self.topic        = topic_prefix
        self.subtopic     = ''
        relpath           = url.path

        # MG compat ?
        self.url          = url

        strpath           = relpath.strip('/')
        words             = strpath.split('/')
        if len(words) > 1 :
           self.subtopic = '.'.join(words[:-1])
           self.topic   += '.' + self.subtopic

        self.topic        = self.topic.replace('..','.')
        self.logger.debug("set_topic_url topic %s" % self.topic )

    def set_topic_usr(self,topic_prefix,subtopic):
        self.topic_prefix = topic_prefix
        self.subtopic     = subtopic
        self.topic        = topic_prefix + '.' + self.subtopic
        self.topic        = self.topic.replace('..','.')

    def start_timer(self):
        self.tbegin = time.time()


    # adjust headers from -headers option

    def trim_headers(self):
        self.logger.debug("trim_headers")

        for k in self.del_headers:
            if k in self.headers : del self.headers[k]

        for k in self.add_headers:
            if k in self.headers : continue
            self.headers[k] = self.add_headers[k]

    def verify_part_suffix(self,filepath):
        filename = os.path.basename(filepath)
        token    = filename.split('.')

        try :  
                 self.suffix = '.' + '.'.join(token[-6:])
                 if token[-1] != self.part_ext :
                    return False,'not right extension',None,None,None

                 self.chunksize     = int(token[-6])
                 self.block_count   = int(token[-5])
                 self.remainder     = int(token[-4])
                 self.current_block = int(token[-3])
                 self.sumflg        = token[-2]

                 if self.current_block >= self.block_count :
                    return False,'current block wrong',None,None,None
                 if self.remainder     >= self.chunksize   :
                    return False,'remainder too big',None,None,None

                 self.length    = self.chunksize
                 self.lastchunk = self.current_block == self.block_count-1
                 self.filesize  = self.block_count * self.chunksize
                 if self.remainder  > 0 :
                    self.filesize  += self.remainder - self.chunksize
                    if self.lastchunk : self.length  = self.remainder

                 lstat     = os.stat(filepath)
                 fsiz      = lstat[stat.ST_SIZE] 

                 if fsiz  != self.length :
                    return False,'wrong file size',None,None,None

                 # compute chksum
                 self.parent.set_sumalgo(self.sumflg)
                 self.sumalgo = self.parent.sumalgo

                 self.sumalgo.set_path(filepath)
                 fp = open(filepath,'rb')
                 i  = 0
                 while i<fsiz :
                       buf = fp.read(self.bufsize)
                       if not buf : break
                       self.sumalgo.update(buf)
                       i  += len(buf)
                 fp.close()

                 if i != fsiz :
                    self.logger.warning("sr_message verify_part_suffix incomplete reading %d %d" % (i,fsiz))
                    return False,'missing data from file', None,None,None

                 # set chksum
                 self.checksum  = self.sumalgo.get_value()


                 # set partstr
                 self.partstr = 'p,%d,%d,%d,%d' %\
                   (self.chunksize,self.block_count,self.remainder,self.current_block)

                 # set sumstr
                 self.sumstr  = '%s,%s' % (self.sumflg,self.checksum)

        except :
                 (stype, svalue, tb) = sys.exc_info()
                 self.logger.error("sr_message/verify_part_suffix Type: %s, Value: %s" % (stype, svalue))
                 return False,'incorrect extension',None,None,None

        return True,'ok',self.suffix,self.partstr,self.sumstr
