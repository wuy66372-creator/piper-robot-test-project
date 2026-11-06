"""One browser-owned live command lease; heartbeats never queue movements."""
import threading
import time

MOVE_TOKENS={'MV_FWD','MV_BACK','MV_LEFT','MV_RIGHT','MV_UP','MV_DOWN','ROTATE_CW','ROTATE_CCW','STILL'}

class LiveLease:
    def __init__(self,clock=time.monotonic,ttl=.4):
        self.clock=clock;self.ttl=ttl;self.lock=threading.RLock()
        self.owner=None;self.tokens=None;self.deadline=0.;self.generation=0;self.sequences={}

    def update(self,client,sequence,tokens):
        if not isinstance(client,str) or not 1<=len(client)<=80:
            raise ValueError('invalid control client')
        if type(sequence)!=int or sequence<0:raise ValueError('invalid sequence')
        if tokens is not None and (len(tokens)!=2 or any(t not in MOVE_TOKENS for t in tokens) or tokens==('STILL','STILL')):
            raise ValueError('continuous mode accepts movement/rotation only')
        with self.lock:
            if self.owner and self.owner!=client and self.tokens and self.clock()<self.deadline:
                raise RuntimeError('another browser currently owns continuous control')
            if sequence<=self.sequences.get(client,-1):return False
            if client not in self.sequences and len(self.sequences)>=32:
                raise RuntimeError('too many control clients; restart the service')
            self.sequences[client]=sequence
            changed=self.tokens!=tokens or self.owner!=client
            if changed:self.generation+=1
            self.owner=client;self.tokens=tokens
            self.deadline=self.clock()+self.ttl if tokens else 0.
            return changed

    def cancel(self):
        with self.lock:
            active=self.tokens is not None
            self.tokens=None;self.deadline=0.;self.generation+=1
            return active

    def expire(self):
        with self.lock:
            if self.tokens is not None and self.clock()>=self.deadline:
                self.cancel();return True
            return False

    def current(self):
        with self.lock:
            if self.tokens is None or self.clock()>=self.deadline:return None
            return self.generation,self.tokens

    def valid(self,generation):
        with self.lock:
            return self.tokens is not None and self.generation==generation and self.clock()<self.deadline
