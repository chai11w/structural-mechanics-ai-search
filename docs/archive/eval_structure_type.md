# 结构类型 LLM 识别评测

## single_beam - MISS

- image: `2静定结构\1单跨梁\题目a\1力\13.jpg`
- expected: `单跨梁`
- actual: `{"structure_type": "多跨梁", "confidence": 1.0, "reason": "连续梁，有两个以上跨度"}`

## continuous_beam - OK

- image: `2静定结构\2多跨梁\内力图\题目2\2跨\1力\13.jpg`
- expected: `多跨梁`
- actual: `{"structure_type": "多跨梁", "confidence": 1.0, "reason": "连续梁，有多个跨度和中间支座"}`

## frame - OK

- image: `2静定结构\3钢架\1内力图\题目2\2固定端\4.jpg`
- expected: `钢架`
- actual: `{"structure_type": "钢架", "confidence": 0.95, "reason": "由梁柱组成，有刚结点"}`

## truss - OK

- image: `2静定结构\5桁架\所有杆\题目aa\1方\17.jpg`
- expected: `桁架`
- actual: `{"structure_type": "桁架", "confidence": 1.0, "reason": "由直杆和三角形单元组成"}`

## composite - OK

- image: `2静定结构\6组合结构\题目1\1M\4.jpg`
- expected: `组合结构`
- actual: `{"structure_type": "组合结构", "confidence": 0.95, "reason": "梁与桁架杆件混合组成"}`
