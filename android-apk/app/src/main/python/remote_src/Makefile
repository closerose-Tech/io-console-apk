#############################################################################
# Makefile - 编译 PCI-1762 远程 IO 测试代理程序
#
# 依赖 DAQNavi Linux SDK (默认安装到 /opt/advantech):
#   - 头文件: /opt/advantech/inc/bdaqctrl.h
#   - 动态库: /opt/advantech/libs/libbiodaq.so  (安装脚本已建 /usr/lib/libbiodaq.so 软链)
#############################################################################

CC       ?= gcc
CFLAGS   ?= -O2 -Wall -Wno-unused-result
SDK_DIR  ?= /opt/advantech
SDK_INC   = $(SDK_DIR)/inc
SDK_LIB   = $(SDK_DIR)/libs

TARGET    = bio1762_io

all: $(TARGET)

$(TARGET): bio1762_io.c
	$(CC) $(CFLAGS) -I$(SDK_INC) -o $(TARGET) bio1762_io.c \
	      -L$(SDK_LIB) -Wl,-rpath,$(SDK_LIB) -lbiodaq

clean:
	rm -f $(TARGET)

.PHONY: all clean
