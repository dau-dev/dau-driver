# Makefile for XDMA macOS DriverKit Extension
#
# Builds the DriverKit extension using xcodebuild from the xdma/ Xcode project.

# Variables
PROJECT = xdma.xcodeproj
TARGET = XDMADriver
CONFIGURATION = Debug
SIGNING_SETTINGS = $(if $(DEVELOPMENT_TEAM),DEVELOPMENT_TEAM=$(DEVELOPMENT_TEAM),)
BUILD_SETTINGS = RUN_CLANG_STATIC_ANALYZER=NO $(SIGNING_SETTINGS)

# Output
EXTENSION_NAME = com.dau-dev.xdma.dext

.PHONY: all compile compile-provision compile-unsigned clean help

help:
	@echo "XDMA DriverKit Extension Build"
	@echo ""
	@echo "Targets:"
	@echo "  compile  - Build the .dext bundle via xcodebuild"
	@echo "  compile-provision - Build and allow Xcode to update signing profiles"
	@echo "  compile-unsigned - Compile without code signing for source validation"
	@echo "  clean    - Clean build artifacts"
	@echo ""
	@echo "Prerequisites:"
	@echo "  - Xcode with DriverKit SDK"
	@echo "  - Valid Apple Developer account for code signing"
	@echo "  - Set DEVELOPMENT_TEAM=<team-id> for signed builds if Xcode has no default"

compile:
	xcodebuild -project $(PROJECT) -target $(TARGET) -configuration $(CONFIGURATION) build $(BUILD_SETTINGS)

compile-provision:
	xcodebuild -project $(PROJECT) -target $(TARGET) -configuration $(CONFIGURATION) build $(BUILD_SETTINGS) -allowProvisioningUpdates

compile-unsigned:
	xcodebuild -project $(PROJECT) -target $(TARGET) -configuration $(CONFIGURATION) build $(BUILD_SETTINGS) CODE_SIGNING_ALLOWED=NO

clean:
	xcodebuild -project $(PROJECT) -target $(TARGET) -configuration $(CONFIGURATION) clean
	rm -f *.o
	@echo "Cleaned build artifacts"

all: compile
