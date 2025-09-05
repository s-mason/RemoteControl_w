#!/bin/bash

# 测试 xdotool mousemove_relative  的脚本
# 缓慢移动鼠标，形成可见轨迹

echo "开始测试鼠标移动轨迹..."
echo "5秒后开始，请确保鼠标在屏幕上可见。"
sleep 5

# 设置每次移动的步长（像素）
STEP=10

# 设置每次移动后的延迟（秒）
DELAY=0.1

# 定义移动路径：从左上角开始，顺时针画一个矩形
echo "开始移动：左上 -> 右上 -> 右下 -> 左下 -> 左上"

# 获取屏幕分辨率
SCREEN_WIDTH=$(xdotool getdisplaygeometry | awk '{print $1}')
SCREEN_HEIGHT=$(xdotool getdisplaygeometry | awk '{print $2}')

echo "屏幕分辨率: ${SCREEN_WIDTH}x${SCREEN_HEIGHT}"

# 从左上角开始
xdotool mousemove_relative  0 0
sleep 1

# 向右移动到右上角
for ((x=0; x<=SCREEN_WIDTH; x+=STEP)); do
    xdotool mousemove_relative  $x 0
    sleep $DELAY
done

# 向下移动到右下角
for ((y=0; y<=SCREEN_HEIGHT; y+=STEP)); do
    xdotool mousemove_relative  $SCREEN_WIDTH $y
    sleep $DELAY
done

# 向左移动到左下角
for ((x=SCREEN_WIDTH; x>=0; x-=STEP)); do
    xdotool mousemove_relative  $x $SCREEN_HEIGHT
    sleep $DELAY
done

# 向上移动到左上角
for ((y=SCREEN_HEIGHT; y>=0; y-=STEP)); do
    xdotool mousemove_relative  0 $y
    sleep $DELAY
done

echo "测试完成！鼠标已回到左上角。"
