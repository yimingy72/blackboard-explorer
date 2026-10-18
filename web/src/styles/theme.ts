import type { XProviderProps } from '@ant-design/x';
export function appTheme(reducedMotion: boolean, narrow = false): XProviderProps['theme'] {
  return {
    cssVar: { key: 'bbx' },
    token: {
      colorPrimary:'#386ac0', colorInfo:'#386ac0', colorSuccess:'#28724c', colorWarning:'#96610d', colorError:'#bd3340',
      colorText:'#242a33', colorTextSecondary:'#647082', colorTextPlaceholder:'#697686', colorBgLayout:'#f5f7fa', colorBorder:'#dce1e8',
      fontFamily:'-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
      fontSize:14, controlHeight:narrow ? 44 : 34, controlHeightSM:narrow ? 44 : 28, borderRadius:7, motion:!reducedMotion,
      motionDurationFast:'0.14s', motionDurationMid:'0.18s', motionDurationSlow:'0.24s',
    },
    components: {
      Button:{fontWeight:500,primaryShadow:'none'}, Table:{headerBg:'#f7f9fc',cellPaddingBlock:12,cellPaddingInline:16},
      Listy:{itemPaddingBlock:14,itemPaddingInline:narrow ? 12 : 20},
      Input:{fontSize:narrow ? 16 : 14,fontSizeSM:narrow ? 16 : 14}, Select:{fontSize:narrow ? 16 : 14,fontSizeSM:narrow ? 16 : 14},
      Tabs:{horizontalItemGutter:24}, Form:{itemMarginBottom:12,verticalLabelPadding:'0 0 4px',labelFontSize:13},
      Menu:{itemHeight:38,itemMarginBlock:3,itemBorderRadius:7,iconSize:16,collapsedWidth:60},
      Collapse:{headerPadding:'8px 10px',contentPadding:'0 10px 10px'}, ThoughtChain:{margin:6,marginSM:8},
    },
  };
}
