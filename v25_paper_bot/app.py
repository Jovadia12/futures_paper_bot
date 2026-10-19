import os, json, sqlite3, hmac, hashlib, time
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

DB=os.getenv('BOT_DB','paper.db')
TOKEN=os.getenv('WEBHOOK_TOKEN','')
app=FastAPI(title='V25 paper-only execution gateway')

def db():
 c=sqlite3.connect(DB,timeout=10,isolation_level=None)
 c.execute('PRAGMA journal_mode=WAL')
 c.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, payload TEXT, result TEXT, created INTEGER)')
 c.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
 return c

def get(c,key,default):
 r=c.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
 return json.loads(r[0]) if r else default

def put(c,key,value):
 c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(value)))

class Signal(BaseModel):
 event_id:str=Field(min_length=8,max_length=150)
 symbol:str
 action:str
 qty:int=Field(ge=1,le=100)
 entry:float=Field(gt=0)
 stop:float=Field(gt=0)
 target:float=Field(gt=0)
 timestamp:int

@app.get('/health')
def health():return {'mode':'PAPER_ONLY','live_enabled':False}

@app.get('/status')
def status():
 with db() as c:
  return {'mode':'PAPER_ONLY','account':get(c,'account',{'equity':50000,'day_start':50000,'peak_eod':50000,'floor':48000,'halt':False}), 'position':get(c,'position',None)}

@app.post('/webhook/{token}')
async def webhook(token:str,request:Request):
 if not TOKEN or not hmac.compare_digest(token,TOKEN):raise HTTPException(403,'Forbidden')
 if int(request.headers.get('content-length','0'))>8192:raise HTTPException(413,'Too large')
 raw=await request.body()
 try: s=Signal.model_validate_json(raw)
 except Exception:raise HTTPException(422,'Invalid signal')
 now=int(time.time())
 if abs(now-s.timestamp)>90:raise HTTPException(422,'Stale signal')
 if s.symbol not in ('MNQ','CME_MINI:MNQ1!'):raise HTTPException(422,'Symbol not allowlisted')
 if s.action not in ('buy','sell'):raise HTTPException(422,'Invalid action')
 if s.qty>2:raise HTTPException(422,'Paper cap 2 contracts')
 ny=datetime.now(ZoneInfo('America/New_York'))
 if ny.weekday()>4 or not ((ny.hour==8 and ny.minute>=30) or 9<=ny.hour<14 or (ny.hour==14 and ny.minute<30)):
  raise HTTPException(422,'Outside entry window')
 if s.action=='buy' and not (s.stop<s.entry<s.target):raise HTTPException(422,'Invalid long bracket')
 if s.action=='sell' and not (s.target<s.entry<s.stop):raise HTTPException(422,'Invalid short bracket')
 risk=(abs(s.entry-s.stop)*2.0+8.0)*s.qty
 if risk>200:raise HTTPException(422,'Risk exceeds $200 paper cap')
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  old=c.execute('SELECT result FROM events WHERE id=?',(s.event_id,)).fetchone()
  if old:
   c.execute('COMMIT')
   return {'duplicate':True,'result':json.loads(old[0])}
  acct=get(c,'account',{'equity':50000,'day_start':50000,'peak_eod':50000,'floor':48000,'halt':False,'day':ny.date().isoformat(),'trades':0})
  if acct.get('day')!=ny.date().isoformat():
   acct['peak_eod']=max(acct['peak_eod'],acct['equity'])
   acct['floor']=min(50000,acct['peak_eod']-2000)
   acct.update(day=ny.date().isoformat(),day_start=acct['equity'],trades=0)
  if acct['halt'] or acct['equity']-acct['floor']<risk+400 or acct['equity']-acct['day_start']-risk< -450 or acct.get('trades',0)>=2:
   raise HTTPException(409,'Risk halt or daily trade limit')
  if get(c,'position',None) is not None:raise HTTPException(409,'Position already open')
  position={'side':s.action,'qty':s.qty,'entry':s.entry,'stop':s.stop,'target':s.target,'opened':now,'simulation':True}
  put(c,'position',position)
  acct['trades']=acct.get('trades',0)+1
  put(c,'account',acct)
  result={'accepted':True,'paper_only':True,'position':position,'risk_estimate':round(risk,2)}
  c.execute('INSERT INTO events VALUES (?,?,?,?)',(s.event_id,raw.decode(),json.dumps(result),now))
  c.execute('COMMIT')
 return result

@app.post('/paper/mark/{token}')
def mark(token:str, price:float):
 if not TOKEN or not hmac.compare_digest(token,TOKEN):raise HTTPException(403)
 if price<=0:raise HTTPException(422)
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  p=get(c,'position',None)
  if p is None:return {'position':None}
  exit_price=None
  if p['side']=='buy':
   if price<=p['stop']:exit_price=p['stop']
   elif price>=p['target']:exit_price=p['target']
  else:
   if price>=p['stop']:exit_price=p['stop']
   elif price<=p['target']:exit_price=p['target']
  if exit_price is None:return {'position':p,'unrealized':(price-p['entry'])*(1 if p['side']=='buy' else -1)*2*p['qty']}
  pnl=(exit_price-p['entry'])*(1 if p['side']=='buy' else -1)*2*p['qty']-8*p['qty']
  acct=get(c,'account',{})
  acct['equity']+=pnl
  if acct['equity']-acct['floor']<=400 or acct['equity']-acct['day_start']<=-450:acct['halt']=True
  put(c,'account',acct)
  put(c,'position',None)
  c.execute('COMMIT')
  return {'closed':True,'pnl':round(pnl,2),'equity':round(acct['equity'],2)}
