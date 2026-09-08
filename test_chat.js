// Run: node --test test_chat.js
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function page(fetch){
  const nodes=new Map();
  const node=()=>({value:'',checked:false,disabled:false,style:{},classList:{toggle(){}},textContent:'',innerHTML:'',children:[],appendChild(child){this.children.push(child)},focus(){}});
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},createElement:node,querySelectorAll(){return []}};
  const context=vm.createContext({document,location:{origin:'http://localhost'},fetch,TextDecoder,Date,console});
  vm.runInContext(fs.readFileSync(__dirname+'/static/chat.html','utf8').split('<script>')[1].split('</script>')[0],context);
  document.getElementById('inp').value='Hello';
  document.getElementById('mdl').value='auto';
  document.getElementById('str').checked=true;
  return {context,document};
}
function stream(parts){return new ReadableStream({start(controller){for(const part of parts)controller.enqueue(new TextEncoder().encode(part));controller.close()}})}
const chunk=(delta,finish_reason=null)=>({id:'chatcmpl-test',object:'chat.completion.chunk',created:1,model:'auto',choices:[{index:0,delta,finish_reason}]});
const event=value=>'data: '+JSON.stringify(value)+'\n\n';

test('SSE handles split UTF-8, CRLF, multiple data lines and final unterminated record',async()=>{
  const {context}=page();
  const encoded=new TextEncoder().encode('data: 繁體\r\ndata: 中文\r\n\r\ndata:[DONE]');
  const body=new ReadableStream({start(c){for(const byte of encoded)c.enqueue(Uint8Array.of(byte));c.close()}});
  const events=[];
  for await(const value of context.streamEvents(body))events.push(value);
  assert.deepEqual(events,['繁體\n中文','[DONE]']);
});
for(const [name,parts,message] of [
  ['malformed JSON',['data: {oops}\n\n'],'JSON'],
  ['upstream error',[event({error:{message:'quota unavailable'}})],'quota unavailable'],
  ['truncated stream',[event(chunk({role:'assistant',content:'partial'}))],'Incomplete stream'],
])test(name+' is visible and does not enter assistant history',async()=>{
  const {context,document}=page(async()=>({ok:true,body:stream(parts)}));
  await context.go();
  assert.match(document.getElementById('fv').innerHTML,/failed/);
  assert.match(document.getElementById('db').textContent,new RegExp(message));
  assert.equal(vm.runInContext('H.filter(m=>m.role==="assistant").length',context),0);
  assert.equal(document.getElementById('btn').disabled,false);
});
test('tool call deltas accumulate and response metadata is escaped',async()=>{
  const parts=[event(chunk({role:'assistant'})),event({...chunk({tool_calls:[{index:0,id:'call-1',type:'function',function:{name:'calculator',arguments:'{"x":'}}]}),model:'<img src=x onerror=alert(1)>'}),event(chunk({tool_calls:[{index:0,function:{arguments:'1}'}}]})),event(chunk({},'tool_calls')),'data: [DONE]\n\n'];
  const {context,document}=page(async()=>({ok:true,body:stream(parts)}));
  await context.go();
  assert.equal(vm.runInContext('H[1].tool_calls[0].function.arguments',context),'{"x":1}');
  context.addM('a','safe',{fr:'stop',md:'<img src=x onerror=alert(1)>'});
  assert.match(document.getElementById('msgs').children.at(-1).innerHTML,/&lt;img/);
});
test('Enter and Clear cannot race an active request',async()=>{
  let resolve,calls=0;
  const {context,document}=page(()=>{calls++;return new Promise(r=>{resolve=r})});
  const running=context.go();
  document.getElementById('inp').value='second';
  await context.go();context.clr();
  assert.equal(calls,1);
  assert.equal(vm.runInContext('H.length',context),1);
  resolve({ok:false,status:503,text:async()=>'offline'});
  await running;
});
test('format validator rejects broken token arithmetic and accepts a trailing usage chunk',()=>{
  const {context}=page();
  const invalid={id:'chatcmpl-test',object:'chat.completion',created:1,model:'auto',choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:'four'}}],usage:{prompt_tokens:2,completion_tokens:3,total_tokens:99}};
  assert.equal(context.validateOpenAI(invalid,false).find(check=>check.name==='usage arithmetic').ok,false);
  const chunks=[chunk({role:'assistant',content:'four'}),chunk({},'stop'),{...chunk({}),choices:[],usage:{prompt_tokens:2,completion_tokens:3,total_tokens:5}}];
  chunks._gotDone=true;
  assert.equal(context.validateOpenAI(chunks,true).filter(check=>check.required&&!check.ok).length,0);
});
