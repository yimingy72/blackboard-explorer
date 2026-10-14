import { describe, expect, it } from 'vitest';
import { toolResultView } from './toolResult';
describe('explicit tool results',()=>{
  it('formats native Content and recognizes only its public failure marker',()=>{
    const text=JSON.stringify({error:{code:'invalid_reference'}});
    expect(toolResultView(JSON.stringify([{type:'text',text,additional_properties:{bbx_tool_error:'invalid_reference'}}])).failed).toBe(true);
    expect(toolResultView(JSON.stringify([{type:'text',text}])).failed).toBe(false);
    expect(toolResultView(text).failed).toBe(false);
    expect(toolResultView('[{"type":"text"')).toEqual({text:'[{"type":"text"',failed:false});
  });
  it('does not surface opaque replay fields or image bytes',()=>{
    const result=toolResultView(JSON.stringify([{type:'text',text:'visible',protected_data:'private'},{type:'image',uri:'data:image/png;base64,private'}]));
    expect(result.text).toBe('visible\n[图像内容]');
  });
});
