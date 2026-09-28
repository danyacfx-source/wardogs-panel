(() => {
  const $ = (id) => document.getElementById(id);
  const ids=["ox","oy","tx","ty","weapon"];
  function calc(){const ox=+$('ox').value||0,oy=+$('oy').value||0,tx=+$('tx').value||0,ty=+$('ty').value||0,dx=tx-ox,dy=ty-oy,d=Math.hypot(dx,dy),range=+$('weapon').selectedOptions[0].dataset.range;
    const az=(Math.atan2(dx,dy)*180/Math.PI+360)%360,inside=d<=range;
    $('distance').textContent=`${Math.round(d)} м · ${(d/1000).toFixed(2)} км`;$('azimuth').textContent=`${az.toFixed(1)}°`;
    $('rangeStatus').textContent=inside?'В зоне поражения':'Вне дальности';$('rangeStatus').className=inside?'ok-text':'err';
    $('dx').textContent=`${dx>=0?'+':''}${Math.round(dx)} м`;$('dy').textContent=`${dy>=0?'+':''}${Math.round(dy)} м`;
    $('mil').textContent=inside?Math.round(1570-(d/range)*700):'—';$('rangeFill').style.width=`${Math.min(100,d/range*100)}%`;
  }
  ids.forEach(id=>$(id).addEventListener('input',calc));$('swap').onclick=()=>{[$('ox').value,$('tx').value]=[$('tx').value,$('ox').value];[$('oy').value,$('ty').value]=[$('ty').value,$('oy').value];calc()};
  $('reset').onclick=()=>{$('ox').value=2000;$('oy').value=2000;$('tx').value=2500;$('ty').value=2500;calc()};calc();
})();
