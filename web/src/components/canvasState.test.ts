import { describe, expect, it } from 'vitest';
import { loadCanvasState, saveCanvasState } from './canvasState';
describe('canvas persistence boundaries',()=>{
  it('keeps ordinary and CTF coordinates and tasks separate',()=>{
    const entries=new Map<string,string>(), storage={getItem:(key:string)=>entries.get(key)??null,setItem:(key:string,value:string)=>entries.set(key,value)};
    const state={positions:{F1:{x:325,y:91}},pinned:['F1'],viewport:{x:24,y:72,zoom:.8},collapsed:true};
    expect(saveCanvasState('blackboard','task-a',state,storage)).toBe(true);
    expect(loadCanvasState('blackboard','task-a',storage)).toEqual(state);
    expect(loadCanvasState('blackboard','task-b',storage)).toBeNull();
    expect(loadCanvasState('ctf-board','task-a',storage)).toBeNull();
    expect(saveCanvasState('ctf-board','task-a',state,storage)).toBe(false);
  });
  it('rejects corrupt coordinates and reports unavailable storage',()=>{
    expect(loadCanvasState('blackboard','a',{getItem:()=>'{'})).toBeNull();
    expect(saveCanvasState('blackboard','a',{positions:{F1:{x:NaN,y:1}},pinned:[],collapsed:true},{setItem:()=>{}})).toBe(false);
    expect(saveCanvasState('blackboard','a',{positions:{},pinned:[],collapsed:true},{setItem:()=>{throw Error('quota');}})).toBe(false);
  });
});

describe('progress tree persistence', () => {
  it('stores expansion and positions independently from both other task views', () => {
    const entries = new Map<string, string>();
    const storage = { getItem: (key: string) => entries.get(key) ?? null, setItem: (key: string, value: string) => entries.set(key, value) };
    const state = { positions: { branch_1: { x: 600, y: 90 } }, pinned: ['branch_1'], collapsed: true, expanded: ['goal', 'branch_1'], viewport: { x: 5, y: 10, zoom: .8 } };
    expect(saveCanvasState('progress-tree', 'a', state, storage)).toBe(true);
    expect(loadCanvasState('progress-tree', 'a', storage)).toEqual(state);
    expect(loadCanvasState('blackboard', 'a', storage)).toBeNull();
    expect(loadCanvasState('ctf-board', 'a', storage)).toBeNull();
    expect(loadCanvasState('progress-tree', 'b', storage)).toBeNull();
    expect(saveCanvasState('progress-tree', 'a', { ...state, expanded: ['invalid id'] }, storage)).toBe(false);
  });
});
