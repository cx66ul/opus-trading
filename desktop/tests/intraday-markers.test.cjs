const test=require('node:test');
const assert=require('node:assert/strict');
const {selectMarkers}=require('../../backend/web/intraday-markers.js');
const signal=(time,kind='T05_sell',extra={})=>({available_at:'2026-10-09 '+time+':00',kind,
  rule_id:kind.split('_')[0],active:true,status:'confirmed',mode:'replay',...extra});
test('default hides candidate and auxiliary signals without changing original records',()=>{
  const rows=[signal('10:00','T06_sell'),signal('10:01','T07_sell'),signal('10:02','T01_buy',{status:'candidate'}),signal('10:03')];
  const saved=JSON.stringify(rows);
  assert.equal(selectMarkers(rows).markers.length,1);
  assert.equal(JSON.stringify(rows),saved);
  assert.equal(selectMarkers(rows,true).markers.length,4);
});
test('same direction merges until opposite confirmed; candidates cannot rearm',()=>{
  const rows=[signal('10:00'),signal('10:01'),signal('10:02','T01_buy',{status:'candidate'}),signal('10:03'),signal('10:04','T01_buy'),signal('10:05')];
  assert.deepEqual(selectMarkers(rows).markers.map(s=>s.available_at.slice(11,16)),['10:00','10:04','10:05']);
});
test('same candle groups evidence without backdating the mark',()=>{
  const result=selectMarkers([signal('10:00'),signal('10:00','3sell',{rule_id:'CHAN'})]);
  assert.equal(result.markers.length,1);assert.equal(result.markers[0].members.length,2);
  assert.equal(result.markers[0].available_at,'2026-10-09 10:00:00');
});
test('afternoon and next date reset suppression',()=>{
  const rows=[signal('10:00'),signal('13:01'),signal('13:02'),signal('10:00','T05_sell',{available_at:'2026-10-12 10:00:00'})];
  assert.equal(selectMarkers(rows).markers.length,3);
});
test('invalidated observations appear only by explicit request and stay invalidated',()=>{
  const rows=[signal('10:00','T05_sell',{active:false})];
  assert.equal(selectMarkers(rows).markers.length,0);
  assert.equal(selectMarkers(rows,true).markers.length,0);
  assert.equal(selectMarkers(rows,true,true).markers[0].active,false);
});
test('appending later signals never relocates an earlier point',()=>{
  const rows=[signal('10:00'),signal('10:05'),signal('10:10','T01_buy')];
  assert.deepEqual(selectMarkers(rows).markers[0],selectMarkers([...rows,signal('10:30')]).markers[0]);
});
